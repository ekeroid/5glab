"""
Kubernetes resource manager — Creates and manages per-tenant Triton deployments.

Handles the full lifecycle of tenant compute resources:
  1. PVC for model storage
  2. Job to export YOLOv8n ONNX and populate the PVC
  3. Deployment running Triton Inference Server with GPU
  4. ClusterIP Service exposing Triton's HTTP port

All resources are namespaced under CAMARA_NAMESPACE and named with
the tenant slug for multi-tenant isolation.
"""

import logging

from kubernetes import client, config as k8s_config

from config import CAMARA_NAMESPACE

logger = logging.getLogger(__name__)

_apps_v1: client.AppsV1Api | None = None
_core_v1: client.CoreV1Api | None = None
_batch_v1: client.BatchV1Api | None = None


def init():
    """Initialize k8s client with in-cluster config."""
    global _apps_v1, _core_v1, _batch_v1
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    _apps_v1 = client.AppsV1Api()
    _core_v1 = client.CoreV1Api()
    _batch_v1 = client.BatchV1Api()
    logger.info("Kubernetes client initialized")


def create_instance(tenant_slug: str, manifest: dict):
    """
    Create all k8s resources for a tenant's Triton instance.

    Creates PVC → model-loader Job → Triton Deployment → Service.
    """
    logger.info(f"Creating instance for tenant {tenant_slug}")

    _create_pvc(tenant_slug)
    _create_model_loader_job(tenant_slug)
    _create_triton_deployment(tenant_slug, manifest)
    _create_triton_service(tenant_slug)

    logger.info(f"All resources created for tenant {tenant_slug}")


def _create_pvc(tenant_slug: str):
    """Create a PVC for model storage."""
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-model-store",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": "5Gi"}
            ),
        ),
    )
    try:
        _core_v1.create_namespaced_persistent_volume_claim(CAMARA_NAMESPACE, pvc)
        logger.info(f"  PVC {tenant_slug}-model-store created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  PVC {tenant_slug}-model-store already exists")
        else:
            raise


def _create_model_loader_job(tenant_slug: str):
    """Create a Job that exports YOLOv8n ONNX and populates the model PVC."""
    gpu_toleration = client.V1Toleration(
        key="nvidia.com/gpu", operator="Exists", effect="NoSchedule"
    )
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-model-loader",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1JobSpec(
            backoff_limit=3,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"tenant": tenant_slug, "job": "model-loader"},
                ),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    tolerations=[gpu_toleration],
                    node_selector={"nvidia.com/gpu.present": "true"},
                    containers=[
                        client.V1Container(
                            name="model-loader",
                            image="ultralytics/ultralytics:latest",
                            command=["python3", "/workspace/export_yolov8_onnx.py"],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="model-store",
                                    mount_path="/models",
                                ),
                                client.V1VolumeMount(
                                    name="scripts",
                                    mount_path="/workspace",
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name="model-store",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=f"{tenant_slug}-model-store",
                            ),
                        ),
                        client.V1Volume(
                            name="scripts",
                            config_map=client.V1ConfigMapVolumeSource(
                                name="triton-model-config",
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )
    try:
        _batch_v1.create_namespaced_job(CAMARA_NAMESPACE, job)
        logger.info(f"  Job {tenant_slug}-model-loader created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  Job {tenant_slug}-model-loader already exists")
        else:
            raise


def _create_triton_deployment(tenant_slug: str, manifest: dict):
    """Create Triton Inference Server deployment with GPU."""
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-triton",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"tenant": tenant_slug, "component": "triton"},
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"tenant": tenant_slug, "component": "triton", "app": "edgevision"},
                ),
                spec=client.V1PodSpec(
                    node_selector={"nvidia.com/gpu.present": "true"},
                    tolerations=[
                        client.V1Toleration(
                            key="nvidia.com/gpu",
                            operator="Exists",
                            effect="NoSchedule",
                        ),
                    ],
                    init_containers=[
                        client.V1Container(
                            name="wait-for-model",
                            image="busybox:1.36",
                            command=["sh", "-c",
                                     "until [ -f /models/yolov8n/1/model.onnx ]; do sleep 5; done"],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="model-store",
                                    mount_path="/models",
                                ),
                            ],
                        ),
                    ],
                    containers=[
                        client.V1Container(
                            name="triton",
                            image="nvcr.io/nvidia/tritonserver:24.10-py3",
                            command=["sh", "-c",
                                     "pip install --no-cache-dir fastapi uvicorn 'tritonclient[grpc]' opencv-python-headless && "
                                     "tritonserver "
                                     "--model-repository=/models "
                                     "--log-verbose=0 "
                                     "--strict-model-config=false "
                                     "--model-control-mode=explicit "
                                     "--load-model=yolov8n & "
                                     "sleep 15 && "
                                     "exec uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir /sidecar"],
                            env=[],
                            ports=[
                                client.V1ContainerPort(container_port=8080, name="http"),
                                client.V1ContainerPort(container_port=8000, name="triton-http"),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"nvidia.com/gpu": "1"},
                                limits={"nvidia.com/gpu": "1"},
                            ),
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="model-store",
                                    mount_path="/models",
                                ),
                                client.V1VolumeMount(
                                    name="sidecar-code",
                                    mount_path="/sidecar",
                                ),
                            ],
                            readiness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path="/health",
                                    port=8080,
                                ),
                                initial_delay_seconds=30,
                                period_seconds=5,
                            ),
                            liveness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path="/v2/health/live",
                                    port=8000,
                                ),
                                initial_delay_seconds=30,
                                period_seconds=10,
                            ),
                        ),
                    ],
                    volumes=[
                        client.V1Volume(
                            name="model-store",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=f"{tenant_slug}-model-store",
                            ),
                        ),
                        client.V1Volume(
                            name="sidecar-code",
                            config_map=client.V1ConfigMapVolumeSource(
                                name="infer-sidecar-code",
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )
    try:
        _apps_v1.create_namespaced_deployment(CAMARA_NAMESPACE, deployment)
        logger.info(f"  Deployment {tenant_slug}-triton created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  Deployment {tenant_slug}-triton already exists")
        else:
            raise


def _create_triton_service(tenant_slug: str):
    """Create ClusterIP service for the Triton deployment."""
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-triton",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={"tenant": tenant_slug, "component": "triton"},
            ports=[
                client.V1ServicePort(name="http", port=8000, target_port=8080),
                client.V1ServicePort(name="triton", port=8001, target_port=8000),
            ],
        ),
    )
    try:
        _core_v1.create_namespaced_service(CAMARA_NAMESPACE, svc)
        logger.info(f"  Service {tenant_slug}-triton created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  Service {tenant_slug}-triton already exists")
        else:
            raise


def get_instance_status(tenant_slug: str) -> str:
    """
    Check the status of a tenant's Triton instance.

    Returns: instantiating | ready | failed
    """
    # Check Job status
    try:
        job = _batch_v1.read_namespaced_job(
            f"{tenant_slug}-model-loader", CAMARA_NAMESPACE
        )
        if job.status.failed and job.status.failed > 0:
            return "failed"
        if not job.status.succeeded or job.status.succeeded < 1:
            return "instantiating"
    except client.ApiException as e:
        if e.status == 404:
            return "instantiating"
        raise

    # Check Deployment readiness
    try:
        dep = _apps_v1.read_namespaced_deployment(
            f"{tenant_slug}-triton", CAMARA_NAMESPACE
        )
        ready = dep.status.ready_replicas or 0
        if ready >= 1:
            return "ready"
        return "instantiating"
    except client.ApiException as e:
        if e.status == 404:
            return "instantiating"
        raise


_cluster_ip_cache: dict[str, str] = {}


def get_service_cluster_ip(tenant_slug: str) -> str | None:
    """Get the ClusterIP of a tenant's Triton service (cached)."""
    if tenant_slug in _cluster_ip_cache:
        return _cluster_ip_cache[tenant_slug]
    try:
        svc = _core_v1.read_namespaced_service(
            f"{tenant_slug}-triton", CAMARA_NAMESPACE
        )
        ip = svc.spec.cluster_ip
        _cluster_ip_cache[tenant_slug] = ip
        return ip
    except client.ApiException:
        return None


def delete_instance(tenant_slug: str):
    """Delete all k8s resources for a tenant."""
    _cluster_ip_cache.pop(tenant_slug, None)
    logger.info(f"Deleting resources for tenant {tenant_slug}")
    propagation = client.V1DeleteOptions(propagation_policy="Foreground")

    # Delete deployment
    try:
        _apps_v1.delete_namespaced_deployment(
            f"{tenant_slug}-triton", CAMARA_NAMESPACE, body=propagation
        )
        logger.info(f"  Deployment {tenant_slug}-triton deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete deployment: {e.reason}")

    # Delete service
    try:
        _core_v1.delete_namespaced_service(
            f"{tenant_slug}-triton", CAMARA_NAMESPACE
        )
        logger.info(f"  Service {tenant_slug}-triton deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete service: {e.reason}")

    # Delete job
    try:
        _batch_v1.delete_namespaced_job(
            f"{tenant_slug}-model-loader", CAMARA_NAMESPACE, body=propagation
        )
        logger.info(f"  Job {tenant_slug}-model-loader deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete job: {e.reason}")

    # Delete PVC
    try:
        _core_v1.delete_namespaced_persistent_volume_claim(
            f"{tenant_slug}-model-store", CAMARA_NAMESPACE
        )
        logger.info(f"  PVC {tenant_slug}-model-store deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete PVC: {e.reason}")
