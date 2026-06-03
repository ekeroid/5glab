"""
Configuration — NEF-shim settings via environment variables.

The NEF-shim runs inside the k8s cluster and manages Triton GPU
deployments on behalf of tenants identified by source IP.
"""

import os

CAMARA_NAMESPACE = os.getenv("CAMARA_NAMESPACE", "edgevision")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
EXTERNAL_HOSTNAME = os.getenv("EXTERNAL_HOSTNAME", "camara.5glab.control.lth.se")
