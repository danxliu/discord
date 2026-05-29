import aiohttp
from pydantic import BaseModel

from config import settings


class ServerInfo(BaseModel):
    name: str
    identifier: str


class ServerStats(BaseModel):
    name: str
    state: str
    cpu: str
    memory: str
    disk: str


async def get_servers():
    """
    Fetches the list of servers from the Pelican Application API.
    Raises an Exception if the request fails.
    """
    headers = {
        "Authorization": f"Bearer {settings.pelican_application_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{settings.pelican_base_url}/api/application/servers"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                servers = []
                for server in data.get("data", []):
                    attr = server["attributes"]
                    servers.append(
                        ServerInfo(name=attr["name"], identifier=attr["identifier"])
                    )
                return servers
            else:
                raise Exception(f"Application API Error: {response.status}")


def format_bytes(b: int) -> str:
    """
    Formats bytes into a human-readable string (GB, MB, KB, B).
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024 or unit == "TB":
            return f"{b:.2f} {unit}"
        b /= 1024


async def get_server_stats(server_id, server_name):
    """
    Fetches the live resource usage for a specific server from the Pelican Client API.
    Raises an Exception if the request fails.
    """
    headers = {
        "Authorization": f"Bearer {settings.pelican_client_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{settings.pelican_base_url}/api/client/servers/{server_id}/resources"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                attr = data["attributes"]
                resources = attr["resources"]

                state = attr["current_state"]
                cpu = resources["cpu_absolute"]
                memory_bytes = resources["memory_bytes"]
                disk_bytes = resources["disk_bytes"]

                return ServerStats(
                    name=server_name,
                    state=state.capitalize(),
                    cpu=f"{cpu:.2f}%",
                    memory=format_bytes(memory_bytes),
                    disk=format_bytes(disk_bytes),
                )
            else:
                raise Exception(f"Client API Error: {response.status}")


async def send_power_action(server_id: str, signal: str):
    """
    Sends a power signal to a specific server.
    Signals: start, stop, restart, kill
    """
    headers = {
        "Authorization": f"Bearer {settings.pelican_client_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{settings.pelican_base_url}/api/client/servers/{server_id}/power"
    payload = {"signal": signal}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 204:
                raise Exception(f"Power API Error: {response.status}")
