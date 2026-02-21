"""RunPod pod management service.

Provides start/stop/status operations for the ComfyUI pod via RunPod GraphQL API.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

RUNPOD_API_URL = "https://api.runpod.io/graphql"

# Pod status constants
class PodStatus(str, Enum):
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    PAUSED = "PAUSED"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


def _get_api_key() -> str:
    return os.getenv("RUNPOD_API_KEY", "").strip()


def _get_pod_id() -> str:
    return os.getenv("RUNPOD_POD_ID", "").strip()


async def get_pod_status() -> tuple[PodStatus, Optional[str]]:
    """Get current pod status and ComfyUI URL.

    Returns:
        Tuple of (PodStatus, comfyui_url or None)
    """
    api_key = _get_api_key()
    pod_id = _get_pod_id()

    if not api_key or not pod_id:
        logger.warning("RUNPOD_API_KEY or RUNPOD_POD_ID not set")
        return PodStatus.UNKNOWN, None

    query = """
    query getPod($podId: String!) {
        pod(input: { podId: $podId }) {
            id
            name
            desiredStatus
            lastStatusChange
            runtime {
                uptimeInSeconds
                ports {
                    ip
                    isIpPublic
                    privatePort
                    publicPort
                    type
                }
            }
        }
    }
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                RUNPOD_API_URL,
                json={"query": query, "variables": {"podId": pod_id}},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        pod = data.get("data", {}).get("pod")
        if not pod:
            logger.error("Pod not found: %s", pod_id)
            return PodStatus.UNKNOWN, None

        desired = pod.get("desiredStatus", "UNKNOWN")
        runtime = pod.get("runtime")

        # Build ComfyUI URL if pod is running
        comfyui_url = None
        if runtime and desired == "RUNNING":
            ports = runtime.get("ports", [])
            for port in ports:
                if port.get("privatePort") == 8188 and port.get("isIpPublic"):
                    public_port = port.get("publicPort")
                    ip = port.get("ip")
                    if ip and public_port:
                        comfyui_url = f"http://{ip}:{public_port}"
                        break
            # Fallback: use RunPod proxy URL
            if not comfyui_url:
                comfyui_url = f"https://{pod_id}-8188.proxy.runpod.net"

        status_map = {
            "RUNNING": PodStatus.RUNNING,
            "EXITED": PodStatus.EXITED,
            "PAUSED": PodStatus.PAUSED,
            "DEAD": PodStatus.DEAD,
        }
        status = status_map.get(desired, PodStatus.UNKNOWN)
        logger.info("Pod %s status: %s, comfyui_url: %s", pod_id, status, comfyui_url)
        return status, comfyui_url

    except Exception as exc:
        logger.error("Failed to get pod status: %s", exc)
        return PodStatus.UNKNOWN, None


async def start_pod() -> bool:
    """Start the RunPod pod (resume from stopped state).

    Returns:
        True if start command was sent successfully.
    """
    api_key = _get_api_key()
    pod_id = _get_pod_id()

    if not api_key or not pod_id:
        logger.warning("RUNPOD_API_KEY or RUNPOD_POD_ID not set")
        return False

    mutation = """
    mutation resumePod($podId: String!, $gpuCount: Int!) {
        podResume(input: { podId: $podId, gpuCount: $gpuCount }) {
            id
            desiredStatus
            lastStatusChange
        }
    }
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                RUNPOD_API_URL,
                json={
                    "query": mutation,
                    "variables": {"podId": pod_id, "gpuCount": 1},
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        errors = data.get("errors")
        if errors:
            logger.error("RunPod start error: %s", errors)
            return False

        result = data.get("data", {}).get("podResume")
        if result:
            logger.info("Pod %s started: desiredStatus=%s", pod_id, result.get("desiredStatus"))
            return True

        logger.error("Unexpected RunPod start response: %s", data)
        return False

    except Exception as exc:
        logger.error("Failed to start pod: %s", exc)
        return False


async def stop_pod() -> bool:
    """Stop the RunPod pod (save state, stop billing for GPU).

    Returns:
        True if stop command was sent successfully.
    """
    api_key = _get_api_key()
    pod_id = _get_pod_id()

    if not api_key or not pod_id:
        logger.warning("RUNPOD_API_KEY or RUNPOD_POD_ID not set")
        return False

    mutation = """
    mutation stopPod($podId: String!) {
        podStop(input: { podId: $podId }) {
            id
            desiredStatus
            lastStatusChange
        }
    }
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                RUNPOD_API_URL,
                json={"query": mutation, "variables": {"podId": pod_id}},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        errors = data.get("errors")
        if errors:
            logger.error("RunPod stop error: %s", errors)
            return False

        result = data.get("data", {}).get("podStop")
        if result:
            logger.info("Pod %s stopped: desiredStatus=%s", pod_id, result.get("desiredStatus"))
            return True

        logger.error("Unexpected RunPod stop response: %s", data)
        return False

    except Exception as exc:
        logger.error("Failed to stop pod: %s", exc)
        return False
