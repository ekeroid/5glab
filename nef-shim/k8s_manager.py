"""
Kubernetes resource manager — Creates and manages per-tenant inference deployments.

Uses the self-contained ghcr.io/ekeroid/5glab/edgevision-infer image which
includes Triton + TRT engine builder + gRPC/HTTP sidecar. On first boot,
the image builds the TRT FP16 engine then serves on :50051 (gRPC) and :8080 (HTTP).

All resources are namespaced under CAMARA_NAMESPACE and named with
the tenant slug for multi-tenant isolation.
"""

import logging

from kubernetes import client, config as k8s_config

from config import CAMARA_NAMESPACE

logger = logging.getLogger(__name__)

INFER_IMAGE = "ghcr.io/ekeroid/5glab/edgevision-infer:latest"
IMAGE_PULL_SECRET = "ghcr-secret"

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
    Create all k8s resources for a tenant's inference instance.

    Creates Deployment → Service.
    The image includes the pre-built TRT engine — no PVC needed.
    """
    logger.info(f"Creating instance for tenant {tenant_slug}")

    _cluster_ip_cache.pop(tenant_slug, None)
    _create_deployment(tenant_slug)
    _create_service(tenant_slug)

    logger.info(f"All resources created for tenant {tenant_slug}")


def _create_pvc(tenant_slug: str):
    """Create a PVC for model storage (persists TRT engine across restarts)."""
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-model-store",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": "2Gi"}
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


def _create_deployment(tenant_slug: str):
    """Create inference server deployment with GPU."""
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-infer",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"tenant": tenant_slug, "component": "infer"},
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"tenant": tenant_slug, "component": "infer", "app": "edgevision"},
                ),
                spec=client.V1PodSpec(
                    runtime_class_name="nvidia",
                    image_pull_secrets=[
                        client.V1LocalObjectReference(name=IMAGE_PULL_SECRET),
                    ],
                    node_selector={"nvidia.com/gpu.present": "true"},
                    tolerations=[
                        client.V1Toleration(
                            key="nvidia.com/gpu",
                            operator="Exists",
                            effect="NoSchedule",
                        ),
                    ],
                    containers=[
                        client.V1Container(
                            name="infer",
                            image=INFER_IMAGE,
                            image_pull_policy="Always",
                            ports=[
                                client.V1ContainerPort(container_port=50051, name="grpc"),
                                client.V1ContainerPort(container_port=8080, name="http"),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"nvidia.com/gpu": "1"},
                                limits={"nvidia.com/gpu": "1"},
                            ),
                            volume_mounts=[],
                            readiness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path="/health",
                                    port=8080,
                                ),
                                initial_delay_seconds=15,
                                period_seconds=3,
                            ),
                            liveness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path="/health",
                                    port=8080,
                                ),
                                initial_delay_seconds=30,
                                period_seconds=10,
                            ),
                        ),
                    ],
                    volumes=[],
                ),
            ),
        ),
    )
    try:
        _apps_v1.create_namespaced_deployment(CAMARA_NAMESPACE, deployment)
        logger.info(f"  Deployment {tenant_slug}-infer created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  Deployment {tenant_slug}-infer already exists")
        else:
            raise


def _create_service(tenant_slug: str):
    """Create service exposing gRPC and HTTP ports."""
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-infer",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "edgevision"},
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector={"tenant": tenant_slug, "component": "infer"},
            ports=[
                client.V1ServicePort(name="grpc", port=50051, target_port=50051),
                client.V1ServicePort(name="http", port=8080, target_port=8080),
            ],
        ),
    )
    try:
        _core_v1.create_namespaced_service(CAMARA_NAMESPACE, svc)
        logger.info(f"  Service {tenant_slug}-infer created")
    except client.ApiException as e:
        if e.status == 409:
            logger.info(f"  Service {tenant_slug}-infer already exists")
        else:
            raise


def get_instance_status(tenant_slug: str) -> str:
    """
    Check the status of a tenant's inference instance.

    Returns: instantiating | ready | failed
    """
    try:
        dep = _apps_v1.read_namespaced_deployment(
            f"{tenant_slug}-infer", CAMARA_NAMESPACE
        )
        ready = dep.status.ready_replicas or 0
        if ready >= 1:
            return "ready"
        return "instantiating"
    except client.ApiException as e:
        if e.status == 404:
            return "instantiating"
        raise


def get_instance_status_detail(tenant_slug: str) -> dict:
    """
    Detailed startup status with phase breakdown.

    Returns dict with: status, phase, message, pod_name, timestamps.
    Phases: scheduling | pulling | starting | waiting_ready | ready | failed
    """
    result = {"status": "instantiating", "phase": "scheduling", "message": ""}

    try:
        pods = _core_v1.list_namespaced_pod(
            CAMARA_NAMESPACE,
            label_selector=f"tenant={tenant_slug},component=infer",
        )
    except client.ApiException:
        return result

    if not pods.items:
        result["message"] = "Waiting for pod to be scheduled"
        return result

    pod = pods.items[0]
    result["pod_name"] = pod.metadata.name

    # Check container statuses
    if pod.status.container_statuses:
        cs = pod.status.container_statuses[0]
        if cs.ready:
            result["status"] = "ready"
            result["phase"] = "ready"
            result["message"] = "Inference server running"
            return result

        if cs.state.waiting:
            reason = cs.state.waiting.reason or ""
            if "Pull" in reason or "Image" in reason:
                result["phase"] = "pulling"
                result["message"] = f"Pulling image ({reason})"
            elif "CrashLoopBackOff" in reason:
                result["status"] = "failed"
                result["phase"] = "failed"
                result["message"] = "Container crash loop"
            else:
                result["phase"] = "starting"
                result["message"] = f"Container starting ({reason})"
            return result

        if cs.state.running:
            result["phase"] = "waiting_ready"
            result["message"] = "Container running, waiting for readiness probe"
            return result

    # Pod scheduled but no container status yet
    if pod.status.phase == "Pending":
        # Check conditions for scheduling info
        if pod.status.conditions:
            for cond in pod.status.conditions:
                if cond.type == "PodScheduled" and cond.status == "True":
                    result["phase"] = "pulling"
                    result["message"] = "Pod scheduled, pulling image"
                    return result
        result["message"] = "Waiting for pod to be scheduled"
    elif pod.status.phase == "Running":
        result["phase"] = "waiting_ready"
        result["message"] = "Container running, waiting for readiness probe"

    return result


_cluster_ip_cache: dict[str, str] = {}


def get_service_cluster_ip(tenant_slug: str) -> str | None:
    """Get the ClusterIP of a tenant's inference service (cached)."""
    if tenant_slug in _cluster_ip_cache:
        return _cluster_ip_cache[tenant_slug]
    try:
        svc = _core_v1.read_namespaced_service(
            f"{tenant_slug}-infer", CAMARA_NAMESPACE
        )
        ip = svc.spec.cluster_ip
        _cluster_ip_cache[tenant_slug] = ip
        return ip
    except client.ApiException:
        return None


def get_service_grpc_nodeport(tenant_slug: str) -> int | None:
    """Get the NodePort allocated for the gRPC port."""
    try:
        svc = _core_v1.read_namespaced_service(
            f"{tenant_slug}-infer", CAMARA_NAMESPACE
        )
        for port in svc.spec.ports:
            if port.name == "grpc":
                return port.node_port
    except client.ApiException:
        pass
    return None


def delete_instance(tenant_slug: str):
    """Delete all k8s resources for a tenant."""
    _cluster_ip_cache.pop(tenant_slug, None)
    logger.info(f"Deleting resources for tenant {tenant_slug}")
    propagation = client.V1DeleteOptions(propagation_policy="Foreground")

    try:
        _apps_v1.delete_namespaced_deployment(
            f"{tenant_slug}-infer", CAMARA_NAMESPACE, body=propagation
        )
        logger.info(f"  Deployment {tenant_slug}-infer deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete deployment: {e.reason}")

    try:
        _core_v1.delete_namespaced_service(
            f"{tenant_slug}-infer", CAMARA_NAMESPACE
        )
        logger.info(f"  Service {tenant_slug}-infer deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete service: {e.reason}")

