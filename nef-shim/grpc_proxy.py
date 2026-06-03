"""
gRPC reverse proxy — Tenant-scoped forwarding to inference pods.

Listens on :50051, extracts tenant identity from peer IP (via x-forwarded-for
when behind Envoy), resolves the tenant's inference service ClusterIP,
and forwards Infer/Health RPCs to the pod's gRPC server.
"""

import hashlib
import logging
import time
from concurrent import futures

import grpc

import infer_pb2
import infer_pb2_grpc
import k8s_manager

logger = logging.getLogger(__name__)


def _slug_from_ip(ip: str) -> str:
    return "t-" + hashlib.md5(ip.encode()).hexdigest()[:8]


def _get_peer_ip(context: grpc.ServicerContext) -> str:
    """Extract client IP from metadata (x-forwarded-for) or peer."""
    metadata = dict(context.invocation_metadata())
    xff = metadata.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    peer = context.peer()
    if peer and peer.startswith("ipv4:"):
        return peer.split(":")[1]
    if peer and peer.startswith("ipv6:["):
        return peer.split("]")[0].replace("ipv6:[", "")
    return peer or "unknown"


_channels: dict[str, grpc.Channel] = {}
_stubs: dict[str, infer_pb2_grpc.InferenceServiceStub] = {}
_stub_targets: dict[str, str] = {}


def _get_backend_stub(tenant_slug: str) -> infer_pb2_grpc.InferenceServiceStub | None:
    cluster_ip = k8s_manager.get_service_cluster_ip(tenant_slug)
    if not cluster_ip:
        return None
    target = f"{cluster_ip}:50051"

    if tenant_slug in _stub_targets and _stub_targets[tenant_slug] != target:
        old_target = _stub_targets[tenant_slug]
        if old_target in _channels:
            _channels[old_target].close()
            del _channels[old_target]
        _stubs.pop(tenant_slug, None)

    if tenant_slug not in _stubs:
        _channels[target] = grpc.insecure_channel(
            target,
            options=[
                ("grpc.keepalive_time_ms", 10000),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ],
        )
        _stubs[tenant_slug] = infer_pb2_grpc.InferenceServiceStub(_channels[target])
        _stub_targets[tenant_slug] = target

    return _stubs[tenant_slug]


class InferenceProxyServicer(infer_pb2_grpc.InferenceServiceServicer):
    def Infer(self, request, context):
        peer_ip = _get_peer_ip(context)
        tenant_slug = _slug_from_ip(peer_ip)

        stub = _get_backend_stub(tenant_slug)
        if stub is None:
            context.abort(grpc.StatusCode.UNAVAILABLE, f"No inference service for tenant {tenant_slug}")
            return infer_pb2.InferResponse()

        try:
            start = time.perf_counter()
            response = stub.Infer(request, timeout=30.0)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"gRPC proxy: tenant={tenant_slug} latency={elapsed:.0f}ms")
            return response
        except grpc.RpcError as e:
            context.abort(e.code(), f"Backend error: {e.details()}")
            return infer_pb2.InferResponse()

    def Health(self, request, context):
        peer_ip = _get_peer_ip(context)
        tenant_slug = _slug_from_ip(peer_ip)

        stub = _get_backend_stub(tenant_slug)
        if stub is None:
            return infer_pb2.HealthResponse(live=False, status="no_service")

        try:
            return stub.Health(request, timeout=5.0)
        except grpc.RpcError:
            return infer_pb2.HealthResponse(live=False, status="backend_unreachable")


_server: grpc.Server | None = None


def start_grpc_proxy():
    """Start the gRPC proxy server on :50051 (blocking)."""
    global _server
    _server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    infer_pb2_grpc.add_InferenceServiceServicer_to_server(InferenceProxyServicer(), _server)
    _server.add_insecure_port("[::]:50051")
    _server.start()
    logger.info("gRPC proxy started on :50051")
    return _server


def stop_grpc_proxy():
    if _server:
        _server.stop(grace=5)
        logger.info("gRPC proxy stopped")
