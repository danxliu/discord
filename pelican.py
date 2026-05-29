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
                    servers.append(ServerInfo(
                        name=attr["name"],
                        identifier=attr["identifier"]
                    ))
                return servers
            else:
                raise Exception(f"Application API Error: {response.status}")


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
                memory = resources["memory_bytes"] / (1024 * 1024)  # MB
                disk = resources["disk_bytes"] / (1024 * 1024)  # MB

                state_emoji = "🟢" if state == "running" else "🔴" if state == "offline" else "🟡"
                
                return ServerStats(
                    name=server_name,
                    state=f"{state_emoji} {state.capitalize()}",
                    cpu=f"{cpu:.2f}%",
                    memory=f"{memory:.2f} MB",
                    disk=f"{disk:.2f} MB",
                )
            else:
                raise Exception(f"Client API Error: {response.status}")
