"""
Kubernetes resource manager — runs user-supplied containers on edge zones.

The edge provides a container runtime. Everything else (image, command,
ports, env, resource requests) is supplied by the user via the CAMARA
app manifest. nef-shim only knows how to translate that manifest into
a Deployment + Service, and how to surface pod state back as CAMARA
status fields.

Resources are namespaced under CAMARA_NAMESPACE and named with the
tenant slug for multi-tenant isolation. Multi-cluster: zone1 (local)
and zone2 (remote via kubeconfig).
"""

import logging
import os

from kubernetes import client, config as k8s_config

from config import CAMARA_NAMESPACE, ZONE2_KUBECONFIG

logger = logging.getLogger(__name__)

IMAGE_PULL_SECRET = os.getenv("IMAGE_PULL_SECRET", "ghcr-secret")

ZONE_LOCAL = "lth-5glab-gpu-zone"
ZONE_XERCES = "xerces-cloud-zone"

_apps_v1: client.AppsV1Api | None = None
_core_v1: client.CoreV1Api | None = None

_z2_apps_v1: client.AppsV1Api | None = None
_z2_core_v1: client.CoreV1Api | None = None

_tenant_zone: dict[str, str] = {}
_tenant_manifest: dict[str, dict] = {}
_cluster_ip_cache: dict[str, str] = {}


def init():
    """Initialize k8s clients for both zones."""
    global _apps_v1, _core_v1, _z2_apps_v1, _z2_core_v1
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    _apps_v1 = client.AppsV1Api()
    _core_v1 = client.CoreV1Api()
    logger.info("Zone1 (local) k8s client initialized")

    if os.path.exists(ZONE2_KUBECONFIG):
        z2_config = client.Configuration()
        k8s_config.load_kube_config(config_file=ZONE2_KUBECONFIG, client_configuration=z2_config)
        z2_api_client = client.ApiClient(configuration=z2_config)
        _z2_apps_v1 = client.AppsV1Api(api_client=z2_api_client)
        _z2_core_v1 = client.CoreV1Api(api_client=z2_api_client)
        logger.info(f"Zone2 (xerces) k8s client initialized from {ZONE2_KUBECONFIG}")
    else:
        logger.warning(f"Zone2 kubeconfig not found at {ZONE2_KUBECONFIG}, zone2 disabled")


def _get_clients(zone_id: str) -> tuple:
    """Return (apps_v1, core_v1) for the given zone."""
    if zone_id == ZONE_XERCES:
        if not _z2_apps_v1:
            raise RuntimeError("Zone2 not configured")
        return _z2_apps_v1, _z2_core_v1
    return _apps_v1, _core_v1


# ─── Manifest → k8s spec translation ────────────────────────────────────────

def _container_image(spec: dict) -> str:
    """Resolve full image reference from containerSpec."""
    if "image" in spec:
        return spec["image"]
    registry = spec.get("imageRegistry", "").rstrip("/")
    name = spec.get("imageName", "")
    tag = spec.get("imageTag", "latest")
    if not name:
        raise ValueError("containerSpec must include 'image' or 'imageName'")
    return f"{registry}/{name}:{tag}" if registry else f"{name}:{tag}"


def _container_ports(manifest: dict) -> list[client.V1ContainerPort]:
    """Collect container ports from componentSpec.networkInterfaces."""
    out: list[client.V1ContainerPort] = []
    for comp in manifest.get("componentSpec", []) or []:
        for nic in comp.get("networkInterfaces", []) or []:
            port = nic.get("port")
            if port is None:
                continue
            out.append(client.V1ContainerPort(
                container_port=int(port),
                name=nic.get("name") or f"port-{port}",
                protocol=nic.get("protocol", "TCP"),
            ))
    return out


def _resources(manifest: dict) -> client.V1ResourceRequirements:
    """Translate requiredResources to k8s ResourceRequirements."""
    req = manifest.get("requiredResources", {}) or {}
    cpu = req.get("cpu")
    mem = req.get("memory")  # MiB
    gpu = req.get("gpu")

    requests = {}
    limits = {}
    if cpu is not None:
        requests["cpu"] = str(cpu)
    if mem is not None:
        requests["memory"] = f"{mem}Mi"
        limits["memory"] = f"{mem}Mi"
    if gpu:
        requests["nvidia.com/gpu"] = str(gpu)
        limits["nvidia.com/gpu"] = str(gpu)

    return client.V1ResourceRequirements(
        requests=requests or None,
        limits=limits or None,
    )


def _env(spec: dict) -> list[client.V1EnvVar]:
    """Translate containerSpec.env (dict or list) to V1EnvVar list."""
    env = spec.get("env") or []
    if isinstance(env, dict):
        return [client.V1EnvVar(name=k, value=str(v)) for k, v in env.items()]
    out: list[client.V1EnvVar] = []
    for item in env:
        if isinstance(item, dict) and "name" in item:
            out.append(client.V1EnvVar(name=item["name"], value=str(item.get("value", ""))))
    return out


def _probe(probe_spec: dict | None) -> client.V1Probe | None:
    """Translate a {http: {path, port}, initialDelaySeconds, periodSeconds} probe."""
    if not probe_spec:
        return None
    http = probe_spec.get("http") or probe_spec.get("httpGet")
    if not http:
        return None
    return client.V1Probe(
        http_get=client.V1HTTPGetAction(
            path=http.get("path", "/"),
            port=http.get("port", 8080),
        ),
        initial_delay_seconds=probe_spec.get("initialDelaySeconds", 10),
        period_seconds=probe_spec.get("periodSeconds", 10),
        failure_threshold=probe_spec.get("failureThreshold", 3),
    )


def _service_ports(manifest: dict) -> list[client.V1ServicePort]:
    """Generate Service ports from componentSpec.networkInterfaces."""
    out: list[client.V1ServicePort] = []
    for comp in manifest.get("componentSpec", []) or []:
        for nic in comp.get("networkInterfaces", []) or []:
            port = nic.get("port")
            if port is None:
                continue
            out.append(client.V1ServicePort(
                name=nic.get("name") or f"port-{port}",
                port=int(port),
                target_port=int(port),
                protocol=nic.get("protocol", "TCP"),
            ))
    return out


# ─── Lifecycle ──────────────────────────────────────────────────────────────

def create_instance(tenant_slug: str, manifest: dict, zone_id: str = ZONE_LOCAL):
    """Create Deployment + Service for the user's container on the target zone."""
    logger.info(f"Creating instance for tenant {tenant_slug} on zone {zone_id}")

    _cluster_ip_cache.pop(tenant_slug, None)
    _tenant_zone[tenant_slug] = zone_id
    _tenant_manifest[tenant_slug] = manifest

    _create_deployment(tenant_slug, manifest, zone_id)
    _create_service(tenant_slug, manifest, zone_id)

    logger.info(f"All resources created for tenant {tenant_slug} on zone {zone_id}")


def _create_deployment(tenant_slug: str, manifest: dict, zone_id: str):
    apps_v1, _ = _get_clients(zone_id)

    cspec = manifest.get("containerSpec") or {}
    image = _container_image(cspec)
    ports = _container_ports(manifest)
    resources = _resources(manifest)
    env = _env(cspec)

    container_kwargs = {
        "name": "app",
        "image": image,
        "image_pull_policy": cspec.get("imagePullPolicy", "Always"),
        "ports": ports or None,
        "resources": resources,
        "env": env or None,
    }
    if cspec.get("command"):
        container_kwargs["command"] = list(cspec["command"])
    if cspec.get("args"):
        container_kwargs["args"] = list(cspec["args"])

    readiness = _probe(cspec.get("readinessProbe"))
    liveness = _probe(cspec.get("livenessProbe"))
    if readiness:
        container_kwargs["readiness_probe"] = readiness
    if liveness:
        container_kwargs["liveness_probe"] = liveness

    container = client.V1Container(**container_kwargs)

    pod_spec_kwargs = {
        "containers": [container],
        "image_pull_secrets": [client.V1LocalObjectReference(name=IMAGE_PULL_SECRET)],
    }
    if (manifest.get("requiredResources") or {}).get("gpu"):
        pod_spec_kwargs["runtime_class_name"] = "nvidia"
        pod_spec_kwargs["node_selector"] = {"nvidia.com/gpu.present": "true"}
        pod_spec_kwargs["tolerations"] = [
            client.V1Toleration(key="nvidia.com/gpu", operator="Exists", effect="NoSchedule"),
        ]

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-app",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "camara-edge"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"tenant": tenant_slug, "component": "app"},
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"tenant": tenant_slug, "component": "app", "app": "camara-edge"},
                ),
                spec=client.V1PodSpec(**pod_spec_kwargs),
            ),
        ),
    )
    try:
        apps_v1.create_namespaced_deployment(CAMARA_NAMESPACE, deployment)
        logger.info(f"  Deployment {tenant_slug}-app created on {zone_id} (image={image})")
    except client.ApiException as e:
        if e.status == 409:
            # An older Deployment exists for this tenant (slug = hash of source IP).
            # Replace it — the new manifest may declare a different image, ports,
            # or resources, so silently reusing the old object would mislead the
            # caller. Replace = delete + recreate; image_pull_policy="Always" so
            # the new image is fetched fresh.
            logger.info(f"  Deployment {tenant_slug}-app already exists — replacing")
            apps_v1.delete_namespaced_deployment(
                f"{tenant_slug}-app", CAMARA_NAMESPACE,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            # Wait briefly for the old pods to terminate (Foreground deletion
            # blocks until owned resources are gone).
            apps_v1.create_namespaced_deployment(CAMARA_NAMESPACE, deployment)
            logger.info(f"  Deployment {tenant_slug}-app recreated (image={image})")
        else:
            raise


def _create_service(tenant_slug: str, manifest: dict, zone_id: str):
    _, core_v1 = _get_clients(zone_id)

    ports = _service_ports(manifest)
    if not ports:
        logger.info(f"  No networkInterfaces declared for {tenant_slug}; skipping Service")
        return

    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=f"{tenant_slug}-app",
            namespace=CAMARA_NAMESPACE,
            labels={"tenant": tenant_slug, "app": "camara-edge"},
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector={"tenant": tenant_slug, "component": "app"},
            ports=ports,
        ),
    )
    try:
        core_v1.create_namespaced_service(CAMARA_NAMESPACE, svc)
        logger.info(f"  Service {tenant_slug}-app created on {zone_id} ({len(ports)} port(s))")
    except client.ApiException as e:
        if e.status == 409:
            # Replace so the new port set takes effect, otherwise the old
            # NodePort assignments would shadow whatever the new manifest asked
            # for.
            logger.info(f"  Service {tenant_slug}-app already exists — replacing")
            core_v1.delete_namespaced_service(f"{tenant_slug}-app", CAMARA_NAMESPACE)
            core_v1.create_namespaced_service(CAMARA_NAMESPACE, svc)
            logger.info(f"  Service {tenant_slug}-app recreated ({len(ports)} port(s))")
        else:
            raise


# ─── Status ─────────────────────────────────────────────────────────────────

def get_instance_status(tenant_slug: str) -> str:
    """Return: instantiating | ready | failed."""
    zone_id = _tenant_zone.get(tenant_slug, ZONE_LOCAL)
    apps_v1, _ = _get_clients(zone_id)
    try:
        dep = apps_v1.read_namespaced_deployment(f"{tenant_slug}-app", CAMARA_NAMESPACE)
        ready = dep.status.ready_replicas or 0
        return "ready" if ready >= 1 else "instantiating"
    except client.ApiException as e:
        if e.status == 404:
            return "instantiating"
        raise


PHASE_ORDER = ["scheduling", "pulling", "starting", "running", "ready"]


def _iso(dt) -> str | None:
    """Format a k8s datetime as RFC3339 with 'Z' suffix."""
    if not dt:
        return None
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def _condition(pod, name: str):
    """Return the V1PodCondition with the given type, or None."""
    for c in pod.status.conditions or []:
        if c.type == name:
            return c
    return None


def _scheduling_failed_reason(pod, core_v1) -> str | None:
    """Look up a FailedScheduling event for this pod and return its message."""
    try:
        events = core_v1.list_namespaced_event(
            CAMARA_NAMESPACE,
            field_selector=f"involvedObject.name={pod.metadata.name},reason=FailedScheduling",
            limit=1,
        )
    except client.ApiException:
        return None
    if events.items:
        return events.items[0].message
    return None


def get_instance_status_detail(tenant_slug: str) -> dict:
    """
    Detailed startup status from k8s pod conditions.

    Phases: scheduling | pulling | starting | running | ready | failed.

    The response carries timestamps so clients can show progress without
    polling drift:
      - createdAt:        pod creation
      - scheduledAt:      PodScheduled True transition (None until scheduled)
      - containerStartedAt: container went into 'running' state
      - phaseStartedAt:   when the current phase began
      - phaseOrder:       canonical phase progression for progress bars
    """
    result = {
        "status": "instantiating",
        "phase": "scheduling",
        "message": "",
        "phaseOrder": PHASE_ORDER,
        "createdAt": None,
        "scheduledAt": None,
        "containerStartedAt": None,
        "phaseStartedAt": None,
    }
    zone_id = _tenant_zone.get(tenant_slug, ZONE_LOCAL)
    _, core_v1 = _get_clients(zone_id)

    try:
        pods = core_v1.list_namespaced_pod(
            CAMARA_NAMESPACE,
            label_selector=f"tenant={tenant_slug},component=app",
        )
    except client.ApiException:
        return result

    if not pods.items:
        result["message"] = "Waiting for pod to be scheduled"
        return result

    pod = pods.items[0]
    result["pod_name"] = pod.metadata.name
    result["createdAt"] = _iso(pod.metadata.creation_timestamp)
    result["phaseStartedAt"] = result["createdAt"]

    sched_cond = _condition(pod, "PodScheduled")
    scheduled_at_dt = sched_cond.last_transition_time if sched_cond and sched_cond.status == "True" else None
    result["scheduledAt"] = _iso(scheduled_at_dt)

    cs = pod.status.container_statuses[0] if pod.status.container_statuses else None
    if cs and cs.state.running and cs.state.running.started_at:
        result["containerStartedAt"] = _iso(cs.state.running.started_at)

    if cs:
        if cs.ready:
            result["status"] = "ready"
            result["phase"] = "ready"
            result["message"] = "Container ready"
            ready_cond = _condition(pod, "ContainersReady")
            if ready_cond and ready_cond.status == "True":
                result["phaseStartedAt"] = _iso(ready_cond.last_transition_time)
            elif result["containerStartedAt"]:
                result["phaseStartedAt"] = result["containerStartedAt"]
            return result

        if cs.state.waiting:
            reason = cs.state.waiting.reason or ""
            msg = cs.state.waiting.message or reason
            if "Pull" in reason or "Image" in reason:
                result["phase"] = "pulling"
                result["message"] = f"Pulling image ({reason})"
                if result["scheduledAt"]:
                    result["phaseStartedAt"] = result["scheduledAt"]
            elif "CrashLoopBackOff" in reason:
                result["status"] = "failed"
                result["phase"] = "failed"
                result["message"] = f"Container crash loop: {msg}"
            else:
                result["phase"] = "starting"
                result["message"] = f"Container starting ({reason})"
                if result["scheduledAt"]:
                    result["phaseStartedAt"] = result["scheduledAt"]
            return result

        if cs.state.running:
            result["phase"] = "running"
            result["message"] = "Container running, waiting for readiness probe"
            if result["containerStartedAt"]:
                result["phaseStartedAt"] = result["containerStartedAt"]
            return result

    # No container_statuses yet — pod scheduling / image pulling
    if pod.status.phase == "Pending":
        if scheduled_at_dt:
            result["phase"] = "pulling"
            result["message"] = "Pod scheduled, pulling image"
            result["phaseStartedAt"] = result["scheduledAt"]
        else:
            result["phase"] = "scheduling"
            reason = _scheduling_failed_reason(pod, core_v1)
            result["message"] = reason or "Waiting for pod to be scheduled"
    elif pod.status.phase == "Running":
        result["phase"] = "running"
        result["message"] = "Container running"
        if result["containerStartedAt"]:
            result["phaseStartedAt"] = result["containerStartedAt"]
    elif pod.status.phase == "Failed":
        result["status"] = "failed"
        result["phase"] = "failed"
        result["message"] = pod.status.reason or "Pod failed"

    return result


# ─── Lookups for proxy/endpoint discovery ───────────────────────────────────

def get_tenant_manifest(tenant_slug: str) -> dict | None:
    """Return the manifest the tenant instantiated (for endpoint discovery)."""
    return _tenant_manifest.get(tenant_slug)


def get_tenant_zone(tenant_slug: str) -> str:
    return _tenant_zone.get(tenant_slug, ZONE_LOCAL)


def get_service_cluster_ip(tenant_slug: str) -> str | None:
    """Get the ClusterIP of a tenant's Service (cached)."""
    if tenant_slug in _cluster_ip_cache:
        return _cluster_ip_cache[tenant_slug]
    zone_id = _tenant_zone.get(tenant_slug, ZONE_LOCAL)
    _, core_v1 = _get_clients(zone_id)
    try:
        svc = core_v1.read_namespaced_service(f"{tenant_slug}-app", CAMARA_NAMESPACE)
        ip = svc.spec.cluster_ip
        _cluster_ip_cache[tenant_slug] = ip
        return ip
    except client.ApiException:
        return None


def get_service_node_ports(tenant_slug: str) -> dict[str, int]:
    """Return {port_name: nodePort} for all ports of the tenant's Service."""
    zone_id = _tenant_zone.get(tenant_slug, ZONE_LOCAL)
    _, core_v1 = _get_clients(zone_id)
    try:
        svc = core_v1.read_namespaced_service(f"{tenant_slug}-app", CAMARA_NAMESPACE)
    except client.ApiException:
        return {}
    out: dict[str, int] = {}
    for p in (svc.spec.ports or []):
        if p.node_port:
            out[p.name] = p.node_port
    return out


# ─── Cleanup ────────────────────────────────────────────────────────────────

def delete_instance(tenant_slug: str):
    """Delete all k8s resources for a tenant."""
    _cluster_ip_cache.pop(tenant_slug, None)
    _tenant_manifest.pop(tenant_slug, None)
    zone_id = _tenant_zone.pop(tenant_slug, ZONE_LOCAL)
    apps_v1, core_v1 = _get_clients(zone_id)
    logger.info(f"Deleting resources for tenant {tenant_slug} on zone {zone_id}")
    propagation = client.V1DeleteOptions(propagation_policy="Foreground")

    try:
        apps_v1.delete_namespaced_deployment(
            f"{tenant_slug}-app", CAMARA_NAMESPACE, body=propagation
        )
        logger.info(f"  Deployment {tenant_slug}-app deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete deployment: {e.reason}")

    try:
        core_v1.delete_namespaced_service(f"{tenant_slug}-app", CAMARA_NAMESPACE)
        logger.info(f"  Service {tenant_slug}-app deleted")
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"  Failed to delete service: {e.reason}")
