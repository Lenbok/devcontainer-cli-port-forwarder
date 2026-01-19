import argparse
import asyncio
import json
import os
import subprocess
import time
import sys

VERBOSE = False

# Maxinum time to wait for the container to start
MAX_WAIT_TIME = int(os.getenv("PORT_FORWARDER_MAX_WAIT_TIME", 300))

# Flag to indicate if the server should stop running
STOP_RUNNING = False


def verbose_print(message, display=False):
    if VERBOSE or display:
        with open("/tmp/devcontainer-cli-port-forwarder.log", "a+") as f:
            f.write(f"[*] forwarder -- {message}\n")


async def _expect_container(container_id, field, value):
    cmd = ["docker", "inspect", "-f", "{{" + field + "}}", container_id]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE)
    stdout, _ = await process.communicate()
    return stdout.decode().strip() == value


async def monitor_container(container_id):
    global STOP_RUNNING
    while True:
        container_running = await _expect_container(
            container_id, ".State.Running", "true"
        )
        container_restarting = await _expect_container(
            container_id, ".State.Restarting", "true"
        )
        container_creating = await _expect_container(
            container_id, ".State.Status", "created"
        )
        if not (container_creating or container_restarting) and not container_running:
            STOP_RUNNING = True
            break
        await asyncio.sleep(1)  # Check every second


async def forward_data(source, target):
    while True:
        data = await source.read(4096)
        if not data:
            break
        target.write(data)
        await target.drain()


async def handle_client(reader, writer, args):
    # Setting up the subprocess to run the command
    (container_id, remote_user, port, use_su) = args

    # Now the container is running, proceed with docker exec
    try:
        if use_su:
            socat_command = f"su - {remote_user} -c 'socat - TCP:localhost:{port}'"
        else:
            socat_command = f"socat - TCP:localhost:{port}"

        command = [
            "docker",
            "exec",
            "-i",
            container_id,
            "bash",
            "-c",
            socat_command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        verbose_print(f"Execute: {' '.join(command)}")

        # Give a brief moment for the command to start and potentially fail
        await asyncio.sleep(0.5)

        # Check if the subprocess was successfully started
        if proc.returncode is not None:
            # The process terminated immediately, handle the error
            verbose_print(
                f"Error: subprocess terminated immediately with return code {proc.returncode}"
            )
            # Check if stderr is available and read from it
            if proc.stdout is not None:
                if stdout := await proc.stdout.read():
                    verbose_print(f"Error in subprocess: {stdout.decode()}")

            if proc.stderr is not None:
                if stderr := await proc.stdout.read():
                    verbose_print(f"Error in subprocess: {stderr.decode()}")

            writer.close()
            await writer.wait_closed()
            return
    except OSError as e:
        # Handle errors related to subprocess execution
        verbose_print(f"Error executing subprocess: {e}")
        writer.close()
        await writer.wait_closed()
        return

    # Separate tasks for reading and writing in both directions
    client_to_container = asyncio.create_task(forward_data(reader, proc.stdin))
    container_to_client = asyncio.create_task(forward_data(proc.stdout, writer))

    # Wait for both tasks to complete
    await asyncio.wait(
        [client_to_container, container_to_client], return_when=asyncio.FIRST_COMPLETED
    )

    writer.close()
    await writer.wait_closed()
    proc.terminate()
    verbose_print(f"Termiate process in {container_id} '{command[-1]}'")


async def start_server(container_id: str, remote_user: str, port, use_su: bool):
    host = "0.0.0.0"
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, (container_id, remote_user, port, use_su)),
        host,
        port,
    )

    async with server:
        await server.start_serving()
        verbose_print(f"Listening on {host}:{port}", display=True)
        while not STOP_RUNNING:
            await asyncio.sleep(1)

        server.close()
        await server.wait_closed()
        verbose_print(f"Stop listening {host}:{port}, exited graceflly", display=True)


async def start_all(container_id, remote_user, forward_ports, use_su: bool):
    server_tasks = [
        start_server(container_id, remote_user, port, use_su) for port in forward_ports
    ]
    # Start container monitoring task
    monitor_task = asyncio.create_task(monitor_container(container_id))

    await asyncio.gather(*server_tasks, monitor_task)


def get_container_id(workspace):
    verbose_print("Wait to get container id")
    command = [
        "docker",
        "ps",
        "-q",
        "--filter",
        f"label=devcontainer.local_folder={workspace}",
        "--filter",
        "status=running",
    ]
    result = subprocess.run(command, capture_output=True, text=True)

    verbose_print(f"result {result}")
    start_time = time.time()
    if not result.stdout.strip():
        verbose_print(" ".join(command))

        while result.returncode != 0 or not result.stdout.strip():
            verbose_print("waiting... ")
            time.sleep(1)  # Wait and check again in 1 second
            verbose_print("woke... ")
            if time.time() - start_time > MAX_WAIT_TIME:
                verbose_print(
                    f"Exited: Container did not start within {MAX_WAIT_TIME} seconds."
                )
                raise Exception(f"Exited: Container did not start within {MAX_WAIT_TIME} seconds.")
            else:
                verbose_print("rerunning... ")
                result = subprocess.run(command, capture_output=True, text=True)
                verbose_print("returned... ")
        container_id = result.stdout.strip()
        verbose_print(f"Got container id {container_id}")
        return container_id
    else:
        # return result.stdout.strip()

        verbose_print(
            f"previous devcontainer {result.stdout.strip()} is running, wait for its removal."
        )
        while result.returncode == 0 and result.stdout.strip():
            time.sleep(1)
            if time.time() - start_time > MAX_WAIT_TIME:
                verbose_print(
                    f"Exited: Container did not restart within {MAX_WAIT_TIME} seconds."
                )
                exit(1)
            else:
                result = subprocess.run(command, capture_output=True, text=True)
        # wait for new devcontainer become running
        while result.returncode != 0 or not result.stdout.strip():
            time.sleep(1)
            if time.time() - start_time > MAX_WAIT_TIME:
                verbose_print(
                    f"Exited: Container did not restart within {MAX_WAIT_TIME} seconds."
                )
                exit(1)
            else:
                result = subprocess.run(command, capture_output=True, text=True)
        container_id = result.stdout.strip()
        verbose_print(f"Got container id {container_id}")
        return container_id


def wait_for_contaier_running(container_id):
    start_time = time.time()
    command = ["docker", "inspect", "-f", "{{.State.Running}}", container_id]

    result = subprocess.run(command, capture_output=True, text=True)
    verbose_print("Wait for container to be running")
    while result.returncode != 0 or result.stdout.strip() != "true":
        time.sleep(1)
        if time.time() - start_time > MAX_WAIT_TIME:
            verbose_print(
                f"Exited: Container {container_id} .State.Running did not become true within {MAX_WAIT_TIME} seconds."
            )
            exit(1)
        else:
            result = subprocess.run(command, capture_output=True, text=True)


def _docker_command(command, container_running=True):
    if container_running:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            verbose_print(f"Error: {command} {result.stderr}", display=True)
            exit(1)
        return result.stdout.strip()


def get_remote_user(devcontainer_json, container_id):
    # determine the user to run the command
    remoteUser = "root"
    if devcontainer_json.get("remoteUser"):
        remoteUser = devcontainer_json.get("remoteUser")
    else:
        metadata: list[dict] = json.loads(
            _docker_command(
                [
                    "docker",
                    "inspect",
                    "-f",
                    '{{ index .Config.Labels "devcontainer.metadata" }}',
                    container_id,
                ],
            )
        )

        for item in metadata:
            if metadata_remote_user := item.get("remoteUser"):
                remoteUser = metadata_remote_user
                break

    verbose_print(f"remoteUser: {remoteUser}")
    return remoteUser


def main(use_su: bool = False):
    # parse json with comments
    # ideally use commentjson or pyjosn5
    # but this will introduce dependency
    jsondata = ""
    with open(".devcontainer/devcontainer.json", "r") as f:
        for line in f:
            jsondata += line.split("//")[0]

    verbose_print(jsondata)
    devcontainer_json = json.loads(jsondata)

    forward_ports = devcontainer_json.get("forwardPorts", [])
    if forward_ports:
        workspace = os.path.realpath(os.getcwd())
        verbose_print(f"Forward_ports is {forward_ports}")
        container_id = get_container_id(workspace)
        verbose_print(f"Have now got container id {container_id}")
        # wait_for_contaier_running(container_id)
        # determine the user to run the socat command
        remote_user = get_remote_user(devcontainer_json, container_id)

        asyncio.run(start_all(container_id, remote_user, forward_ports, use_su))
    else:
        verbose_print("No forwardPorts found", display=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DevContainer CLI Port Forwarder - Forward ports from host to devcontainer"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging to /tmp/devcontainer-cli-port-forwarder.log"
    )
    parser.add_argument(
        "--use-su",
        action="store_true",
        help="Use 'su - user -c socat' instead of direct socat command"
    )

    args = parser.parse_args()

    if args.verbose:
        VERBOSE = True

    try:
        main(args.use_su)
    except Exception as e:
        verbose_print(f"An error occurred: {e}")
