import json
import os
import shutil
import subprocess
import time

import psycopg2
import requests
from loguru import logger
from psycopg2 import Error

MATTERMOST_DOCKER_DIR = "/app/mattermost-docker"
COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.without-nginx.yml"]
MATTERMOST_DB_HOST = "localhost"
MATTERMOST_DB_DATABASE = "mattermost"
MATTERMOST_DB_USER = "mmuser"
MATTERMOST_DB_PASSWORD = "mmuser_password"
MATTERMOST_DB_PORT = "5433"

MATTERMOST_STATUS_DIR = "/app/mattermost-docker-bk"
SAM_HARRY_CHANNEL_ID = "m3d6byju9ig4dneosajg9hu1be"
HARRY_ID = "p11jse4oa3biikeeefcuggns9o"
PHOENIX_CHANNEL_ID = "6xntskboopfwxysbdebkzqyckh"
ALEX_ID = "1hx8frqxjfdhuqzkp4yt511sho"
TEAM_NAME = "neuralforge"
SAM_ACCOUNT = {
    "username": "sam.oneill@neuralforge.ai",
    "password": "password",
}
ADMIN_ACCOUNT = {
    "username": "admin@test.com",
    "password": "password",
}
USERS = {
    "sam": "sam.oneill@neuralforge.ai",
    "alex": "alex.rivera@neuralforge.ai",
    "mike": "mike.santos@neuralforge.ai",
    "sofia": "sofia.garcia@neuralforge.ai",
}
DEFAULT_PASSWORD = "password"

MATTERMOST_API_URL = "http://127.0.0.1:8065"
MATTERMOST_READY_TIMEOUT = 120.0
MATTERMOST_READY_POLL_INTERVAL = 2.0


class MattermostSetupError(RuntimeError):
    """A Mattermost setup step failed while a task was initializing.

    Raised rather than returned so a hook that forgets to check a return value
    cannot silently succeed: it propagates out of initialize_task_hook ->
    BaseTask.initialize_task -> POST /task/init, which turns it into a 500 with
    a readable detail message.
    """


class MattermostCLI:
    def __init__(
        self, container_id: str = "mattermost-docker", server_url: str = "http://127.0.0.1:8065"
    ):
        self.container_id = container_id
        self.server_url = server_url
        self.auth_name = "cli-session"

    def _exec_in_container(self, command: str) -> tuple[int, str, str]:
        """Execute a command inside the Mattermost container."""
        full_command = [
            "docker",
            "exec",
            "-i",
            f"{self.container_id}-mattermost-1",
            "bash",
            "-c",
            command,
        ]
        result = subprocess.run(full_command, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    def _login_attempt(self, username: str, password: str) -> bool:
        """
        Attempt to login to Mattermost using mmctl.

        Args:
            username: The user's email/username
            password: The user's password

        Returns:
            bool: True if login successful, False otherwise
        """
        command = f'''
echo "{password}" > /tmp/mmctl_pass.txt && \
mmctl auth login {self.server_url} \
    --name {self.auth_name} \
    --username {username} \
    --password-file /tmp/mmctl_pass.txt && \
rm -f /tmp/mmctl_pass.txt
'''
        returncode, stdout, stderr = self._exec_in_container(command)
        return returncode == 0 and "stored" in stdout

    def _reset_user_password(self, target_username: str, new_password: str) -> bool:
        """
        Reset a user's password using admin account.

        Args:
            target_username: The username whose password to reset
            new_password: The new password to set

        Returns:
            bool: True if password reset successful, False otherwise
        """
        admin_username = ADMIN_ACCOUNT["username"]
        admin_password = ADMIN_ACCOUNT["password"]

        # First login as admin
        admin_auth_name = "admin-session"
        login_command = f'''
echo "{admin_password}" > /tmp/mmctl_pass.txt && \
mmctl auth login {self.server_url} \
    --name {admin_auth_name} \
    --username {admin_username} \
    --password-file /tmp/mmctl_pass.txt && \
rm -f /tmp/mmctl_pass.txt
'''
        returncode, stdout, stderr = self._exec_in_container(login_command)
        if returncode != 0 or "stored" not in stdout:
            logger.error(f"Failed to login as admin: {stderr or stdout}")
            return False

        # Reset the target user's password
        reset_command = f'mmctl user change-password {target_username} --password "{new_password}"'
        returncode, stdout, stderr = self._exec_in_container(reset_command)

        # Cleanup admin session
        self._exec_in_container(f"mmctl auth delete {admin_auth_name}")

        if returncode == 0:
            logger.info(f"Password reset successful for {target_username}")
            return True
        else:
            logger.error(f"Failed to reset password: {stderr or stdout}")
            return False

    def login(self, username: str, password: str | None = None) -> bool:
        """
        Login to Mattermost using mmctl. If login fails, attempt to reset
        the user's password using admin account and retry.

        Args:
            username: The user's email/username
            password: The user's password

        Returns:
            bool: True if login successful.

        Raises:
            MattermostSetupError: if login and the password-reset fallback both failed.
        """
        # First attempt
        if password is not None and self._login_attempt(username, password):
            logger.info(f"✓ Logged in as {username}")
            return True

        logger.warning(f"Login failed for {username}, attempting password reset...")

        # Try to reset password using admin account
        if not self._reset_user_password(username, DEFAULT_PASSWORD):
            logger.error(f"✗ Failed to reset password for {username}")
            raise MattermostSetupError(f"could not reset password for {username}")

        # Retry login after password reset
        if self._login_attempt(username, DEFAULT_PASSWORD):
            logger.info(f"✓ Logged in as {username} after password reset")
            return True

        logger.error(f"✗ Login still failed after password reset for {username}")
        raise MattermostSetupError(f"mmctl login failed for {username}")

    def logout(self) -> bool:
        """Clean up authentication credentials."""
        command = f"mmctl auth delete {self.auth_name}"
        returncode, stdout, stderr = self._exec_in_container(command)
        return returncode == 0

    def create_channel(
        self,
        team: str,
        channel_name: str,
        display_name: str,
        private: bool = False,
        purpose: str = "",
        header: str = "",
    ) -> bool:
        """
        Create a new channel.

        Args:
            team: Team name or ID
            channel_name: Channel name (slug, no spaces)
            display_name: Channel display name
            private: Whether the channel is private
            purpose: Channel purpose (optional)
            header: Channel header (optional)

        Returns:
            bool: True if channel created successfully
        """
        command = f'''mmctl channel create \
    --team "{team}" \
    --name "{channel_name}" \
    --display-name "{display_name}"'''

        if private:
            command += " --private"
        if purpose:
            command += f' --purpose "{purpose}"'
        if header:
            command += f' --header "{header}"'

        returncode, stdout, stderr = self._exec_in_container(command)

        if returncode == 0 and "successfully created" in stdout:
            logger.info(f"Channel '{channel_name}' created successfully")
            return True
        logger.error(f"Failed to create channel: {stderr or stdout}")
        raise MattermostSetupError(
            f"failed to create channel '{team}:{channel_name}': {(stderr or stdout).strip()[:200]}"
        )

    def send_message(self, team: str, channel: str, message: str, reply_to: str = None) -> bool:
        """
        Send a message to a channel.

        Args:
            team: Team name or ID
            channel: Channel name
            message: Message content
            reply_to: Post ID to reply to (optional)

        Returns:
            bool: True if message sent successfully
        """
        # Escape special characters in the message
        escaped_message = message.replace('"', '\\"').replace("$", "\\$")

        command = f'mmctl post create {team}:{channel} --message "{escaped_message}"'

        if reply_to:
            command += f' --reply-to "{reply_to}"'

        returncode, stdout, stderr = self._exec_in_container(command)

        if returncode == 0:
            logger.info(f"Message sent to {team}:{channel}")
            return True
        logger.error(f"Failed to send message: {stderr or stdout}")
        raise MattermostSetupError(
            f"failed to send message to {team}:{channel}: {(stderr or stdout).strip()[:200]}"
        )

    def add_users_to_channel(self, team: str, channel: str, users: list[str]) -> bool:
        """
        Add users to a channel.

        Args:
            team: Team name or ID
            channel: Channel name
            users: List of usernames or emails

        Returns:
            bool: True if users added successfully
        """
        users_str = " ".join(users)
        command = f"mmctl channel users add {team}:{channel} {users_str}"

        returncode, stdout, stderr = self._exec_in_container(command)

        if returncode == 0:
            logger.info(f"Users added to {team}:{channel}")
            return True
        logger.error(f"Failed to add users: {stderr or stdout}")
        raise MattermostSetupError(
            f"failed to add users to {team}:{channel}: {(stderr or stdout).strip()[:200]}"
        )

    def list_channels(self, team: str) -> list[str]:
        """
        List all channels in a team.

        Args:
            team: Team name or ID

        Returns:
            list: List of channel names
        """
        command = f"mmctl channel list {team}"

        returncode, stdout, stderr = self._exec_in_container(command)

        if returncode == 0:
            # Parse channel list from output
            lines = stdout.strip().split("\n")
            channels = [
                line.strip() for line in lines if line.strip() and not line.startswith("There are")
            ]
            return channels
        return []


# Convenience function for quick operations
def mattermost_operation(
    container_id: str, username: str, password: str, operation: str, **kwargs
) -> bool:
    """
    Perform a Mattermost operation.

    Args:
        container_id: Docker container ID (e.g., 'mattermost-docker')
        username: Mattermost username/email
        password: Mattermost password
        operation: One of 'create_channel', 'send_message', 'add_users'
        **kwargs: Operation-specific arguments

    Returns:
        bool: True if operation successful
    """
    cli = MattermostCLI(container_id)

    if not cli.login(username, password):
        return False

    try:
        if operation == "create_channel":
            return cli.create_channel(
                team=kwargs.get("team"),
                channel_name=kwargs.get("channel_name"),
                display_name=kwargs.get("display_name"),
                private=kwargs.get("private", False),
                purpose=kwargs.get("purpose", ""),
                header=kwargs.get("header", ""),
            )
        elif operation == "send_message":
            return cli.send_message(
                team=kwargs.get("team"),
                channel=kwargs.get("channel"),
                message=kwargs.get("message"),
                reply_to=kwargs.get("reply_to"),
            )
        elif operation == "add_users":
            return cli.add_users_to_channel(
                team=kwargs.get("team"),
                channel=kwargs.get("channel"),
                users=kwargs.get("users", []),
            )
        else:
            logger.error(f"Unknown operation: {operation}")
            return False
    finally:
        cli.logout()


def copytree_with_ownership(src, dst):
    subprocess.run(["cp", "-rp", src, dst], check=True)


def connect_to_postgres():
    try:
        connection = psycopg2.connect(
            host=MATTERMOST_DB_HOST,
            database=MATTERMOST_DB_DATABASE,
            user=MATTERMOST_DB_USER,
            password=MATTERMOST_DB_PASSWORD,
            port=MATTERMOST_DB_PORT,
        )
        cursor = connection.cursor()
        logger.info("Connected to PostgreSQL database successfully!")
        return connection, cursor
    except Error:
        logger.warning("Error connecting to PostgreSQL database")
        return None, None


def get_table_schema(table_name="posts"):
    """Get the schema/structure of the posts table from the PostgreSQL database."""
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None

    try:
        cursor.execute(f"""
            SELECT 
                column_name,
                data_type,
                character_maximum_length,
                is_nullable,
                column_default
            FROM 
                information_schema.columns
            WHERE 
                table_name = '{table_name}'
            ORDER BY 
                ordinal_position;
        """)

        schema = cursor.fetchall()
        return schema

    except Exception as e:
        print(f"Error fetching schema: {e}")
        return None
    finally:
        cursor.close()
        connection.close()


def show_all_tables():
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None
    cursor.execute("SELECT * FROM information_schema.tables")
    tables = cursor.fetchall()
    connection.close()
    return tables


def get_latest_messages():
    """Get the latest messages from the PostgreSQL database."""
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None
    cursor.execute("SELECT * FROM posts ORDER BY createat DESC")
    messages = cursor.fetchall()
    connection.close()
    return messages


def get_channel_info(channel_id: str = None, channel_name: str = None):
    """Get the channel information from the PostgreSQL database."""
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None
    if channel_id is not None:
        cursor.execute(f"SELECT * FROM channels WHERE id = '{channel_id}'")
    elif channel_name is not None:
        cursor.execute(f"SELECT * FROM channels WHERE LOWER(name) = LOWER('{channel_name}')")
    else:
        return None
    channel = cursor.fetchone()
    connection.close()
    return channel


def get_file_info(file_id: str, return_path: bool = False):
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None
    cursor.execute(f"SELECT * FROM fileinfo WHERE id = '{file_id}'")
    file = cursor.fetchone()
    connection.close()
    if return_path:
        relative_path = file[6]
        return os.path.join(MATTERMOST_DOCKER_DIR, "volumes/app/mattermost/data/", relative_path)
    return file


def is_user_in_channel(user_id: str, channel_id: str):
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return False
    cursor.execute(
        f"SELECT * FROM channelmembers WHERE userid = '{user_id}' AND channelid = '{channel_id}'"
    )
    member = cursor.fetchone()
    connection.close()
    return member is not None


def get_user_id_by_email(email: str) -> str | None:
    """Get user ID by email address."""
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return None
    cursor.execute(f"SELECT id FROM users WHERE email = '{email}'")
    user = cursor.fetchone()
    connection.close()
    return user[0] if user else None


def get_users_in_channel(channel_id: str):
    connection, cursor = connect_to_postgres()
    if connection is None or cursor is None:
        return False
    cursor.execute(f"SELECT * FROM channelmembers WHERE channelid = '{channel_id}'")
    members = cursor.fetchall()
    connection.close()
    return members


def _patch_mattermost_config():
    """Patch Mattermost config.json to disable session expiry and idle timeout,
    and to allow WebSocket connections from the Android emulator.

    Called after copying the backup but before docker compose up, so Mattermost
    starts with session settings that effectively never expire.

    The Android Mattermost app connects to ws://10.0.2.2:8065/api/v4/websocket
    and sends Origin: http://10.0.2.2:8065. Mattermost's WebSocket upgrader
    rejects the upgrade with HTTP 400 ("URL Blocked because of CORS") unless
    the Origin matches SiteURL or AllowCorsFrom. The shipped config has both
    empty, so the upgrade fails and the app shows "The server is not reachable"
    and never receives live updates (e.g. channels created during init by
    mmctl). Pinning SiteURL/AllowCorsFrom here unblocks the upgrade.
    """
    config_path = os.path.join(
        MATTERMOST_DOCKER_DIR, "volumes", "app", "mattermost", "config", "config.json"
    )
    try:
        with open(config_path) as f:
            config = json.load(f)

        svc = config.setdefault("ServiceSettings", {})
        svc["SessionLengthWebInDays"] = 36500
        svc["SessionLengthWebInHours"] = 876000
        svc["SessionLengthMobileInDays"] = 36500
        svc["SessionLengthMobileInHours"] = 876000
        svc["SessionLengthSSOInDays"] = 36500
        svc["SessionLengthSSOInHours"] = 876000
        svc["SessionIdleTimeoutInMinutes"] = 0
        svc["ExtendSessionLengthWithActivity"] = True
        svc["SiteURL"] = "http://10.0.2.2:8065"
        svc["AllowCorsFrom"] = "*"
        # Let MM fetch our locally-hosted task images (http://10.0.2.2:6800/task-asset/...)
        # for inline-image metadata; the default empty allowlist makes its SSRF filter
        # block the private 10.0.2.2 IP, so the app renders a spinner instead of the image.
        svc["AllowedUntrustedInternalConnections"] = "10.0.2.2"

        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)

        logger.info(
            "Patched Mattermost config: sessions never expire; "
            "SiteURL/AllowCorsFrom set for emulator WebSocket"
        )
    except Exception as e:
        logger.warning(f"Failed to patch Mattermost config (non-fatal): {e}")


def _extend_session_expiry():
    """Extend all existing Mattermost session expiry times in the database.

    The Android emulator snapshot stores a cached auth token, but the PostgreSQL
    backup has session records with fixed expiry timestamps. As real time advances
    past those timestamps, sessions become invalid and the app logs the user out.
    This function extends all sessions to 100 years from now and refreshes
    lastactivityat to prevent idle-timeout invalidation.

    MUST be called while only PostgreSQL is running, before the Mattermost server
    container starts. Mattermost runs a session-cleanup pass shortly after it
    connects to the DB and will DELETE any rows where expiresat is in the past,
    racing against this UPDATE. Starting postgres first eliminates the race.
    """
    max_retries = 10
    for attempt in range(max_retries):
        connection, cursor = connect_to_postgres()
        if connection is not None and cursor is not None:
            try:
                cursor.execute("""
                    UPDATE sessions
                    SET expiresat = (EXTRACT(EPOCH FROM (NOW() + INTERVAL '36500 days')) * 1000)::bigint,
                        lastactivityat = (EXTRACT(EPOCH FROM NOW()) * 1000)::bigint
                    WHERE expiresat > 0
                """)
                rows_updated = cursor.rowcount
                connection.commit()
                logger.info(f"Extended expiry for {rows_updated} Mattermost session(s)")
                return True
            except Exception as e:
                logger.error(f"Failed to extend session expiry: {e}")
                return False
            finally:
                cursor.close()
                connection.close()
        else:
            logger.debug(
                f"Waiting for PostgreSQL to be ready (attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(2)

    logger.error("PostgreSQL not ready after max retries, could not extend session expiry")
    return False


def _mattermost_api_ready() -> bool:
    """True when the Mattermost API is up and its auth path answers.

    ``GET /system/ping`` only proves the HTTP layer is listening; login can
    still be rejected while Mattermost finishes booting. ``POST /users/login``
    is the exact call mmctl makes, so accept 200 (valid credentials) or 401
    (credentials rejected => the API and its database are alive). Any other
    status or a connection error means "not ready yet".
    """
    try:
        ping = requests.get(f"{MATTERMOST_API_URL}/api/v4/system/ping", timeout=3)
        if ping.status_code != 200 or ping.json().get("status") != "OK":
            return False
    except (requests.RequestException, ValueError):
        return False

    try:
        resp = requests.post(
            f"{MATTERMOST_API_URL}/api/v4/users/login",
            # ``login_id`` is the field the official clients use (verified in
            # the 10.5.2 webapp bundle); ``username`` would be a 400.
            json={"login_id": SAM_ACCOUNT["username"], "password": SAM_ACCOUNT["password"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=5,
        )
    except requests.RequestException:
        return False

    if resp.status_code not in (200, 401):
        return False
    if resp.status_code == 200:
        token = resp.headers.get("Token")
        if token:  # do not leak a session row just to probe
            try:
                requests.post(
                    f"{MATTERMOST_API_URL}/api/v4/users/logout",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3,
                )
            except requests.RequestException:
                pass
    return True


def wait_for_mattermost_ready(
    timeout: float = MATTERMOST_READY_TIMEOUT,
    poll_interval: float = MATTERMOST_READY_POLL_INTERVAL,
) -> bool:
    """Poll the Mattermost API until it accepts logins (or *timeout* elapses).

    ``docker compose up -d`` returns as soon as the containers are created;
    the API rejects logins for another ~12s (measured). Every task hook runs
    mmctl commands immediately afterwards, so returning early means the
    channels and messages they create are silently lost.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        if _mattermost_api_ready():
            logger.info(f"Mattermost API ready after {attempt} probe(s)")
            return True
        if time.monotonic() >= deadline:
            logger.error(
                f"Mattermost API not ready after {timeout:.0f}s "
                f"({attempt} probes, {MATTERMOST_API_URL})"
            )
            return False
        logger.debug(
            f"Mattermost API not ready yet (probe {attempt}); retrying in {poll_interval}s"
        )
        time.sleep(poll_interval)


def start_mattermost_backend(mattermost_backend_status_dir=MATTERMOST_STATUS_DIR):
    """Start the Mattermost backend.

    Returns only once the API accepts logins, so callers can immediately run
    mmctl commands against it.

    Raises:
        MattermostSetupError: if the stack fails to start or the API never
            becomes ready within ``MATTERMOST_READY_TIMEOUT``.
    """
    status = get_mattermost_backend_status()
    if status in ("running", "partial"):
        logger.info("Mattermost backend is already running, stop and reset it to default")
        stop_mattermost_backend()
    shutil.rmtree(MATTERMOST_DOCKER_DIR, ignore_errors=True)

    try:
        # mattermost backend requires 2000:2000 permission, need to preserve
        copytree_with_ownership(mattermost_backend_status_dir, MATTERMOST_DOCKER_DIR)

        # Patch config before starting so Mattermost reads updated session settings
        _patch_mattermost_config()

        # Start PostgreSQL first (without Mattermost) so we can extend session
        # expiry before Mattermost connects and prunes "expired" rows. If both
        # containers come up together, Mattermost's startup cleanup races our
        # UPDATE and wins on slow-start runs, leaving the Android app with a
        # token whose backing session row has been deleted (login screen).
        pg_cmd = ["docker", "compose"] + COMPOSE_FILES + ["up", "-d", "postgres"]
        subprocess.run(
            pg_cmd, cwd=MATTERMOST_DOCKER_DIR, capture_output=True, text=True, check=True
        )

        # Extend existing session expiry while only postgres is running.
        if not _extend_session_expiry():
            logger.warning(
                "Could not extend Mattermost session expiry; the Android app "
                "may end up logged out (see fix c532c19)"
            )

        # Now bring up the Mattermost server.
        cmd = ["docker", "compose"] + COMPOSE_FILES + ["up", "-d"]
        result = subprocess.run(
            cmd, cwd=MATTERMOST_DOCKER_DIR, capture_output=True, text=True, check=True
        )

        # docker compose up -d returns before the API accepts logins; task
        # hooks run mmctl commands immediately afterwards.
        if not wait_for_mattermost_ready():
            raise MattermostSetupError(
                f"Mattermost API at {MATTERMOST_API_URL} not ready after "
                f"{MATTERMOST_READY_TIMEOUT:.0f}s"
            )
        logger.info("Mattermost backend started successfully")
        logger.debug(f"Docker compose output: {result.stdout}\n{result.stderr}")

        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start Mattermost backend: {e}")
        logger.error(f"Error output: {e.stderr}")
        raise MattermostSetupError(f"docker compose up failed: {e.stderr or e}") from e
    except MattermostSetupError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error starting Mattermost backend: {e}")
        raise MattermostSetupError(f"unexpected error starting Mattermost backend: {e}") from e


def stop_mattermost_backend():
    """Stop the Mattermost backend."""
    try:
        status = get_mattermost_backend_status()
        if status == "stopped":
            return True
        cmd = ["docker", "compose", "down"]
        result = subprocess.run(
            cmd, cwd=MATTERMOST_DOCKER_DIR, capture_output=True, text=True, check=True
        )
        logger.info("Mattermost backend stopped successfully")
        logger.debug(f"Docker compose output: {result.stdout}\n{result.stderr}")

        shutil.rmtree(MATTERMOST_DOCKER_DIR)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stop Mattermost backend: {e}")
        logger.error(f"Error output: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error stopping Mattermost backend: {e}")
        return False


def restart_mattermost_backend():
    """Restart the Mattermost backend."""
    logger.info("Restarting Mattermost backend...")

    # Stop the backend first
    if not stop_mattermost_backend():
        logger.error("Failed to stop Mattermost backend during restart")
        return False

    # Wait a moment for services to fully stop

    time.sleep(2)

    # Start the backend (raises MattermostSetupError on failure)
    start_mattermost_backend()

    logger.info("Mattermost backend restarted successfully")
    return True


def get_mattermost_backend_status():
    """Get the status of the Mattermost backend."""
    if not os.path.exists(MATTERMOST_DOCKER_DIR):
        return "stopped"
    try:
        cmd = ["docker", "compose", "ps", "--format", "json"]
        result = subprocess.run(
            cmd, cwd=MATTERMOST_DOCKER_DIR, capture_output=True, text=True, check=True
        )

        if not result.stdout.strip():
            return "stopped"

        services = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    service = json.loads(line)
                    services.append(service)
                except json.JSONDecodeError:
                    continue

        logger.info(f"Mattermost services: {services}")
        if not services:
            return "stopped"

        # Check if all services are running
        running_services = 0
        total_services = len(services)

        for service in services:
            state = service.get("State", "").lower()
            if state == "running":
                running_services += 1

        if running_services == total_services and total_services > 0:
            return "running"
        elif running_services > 0:
            return "partial"
        else:
            return "stopped"

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get Mattermost backend status: {e}")
        return "error"
    except Exception as e:
        logger.error(f"Unexpected error getting Mattermost backend status: {e}")
        return "error"


def get_mattermost_services_info():
    """Get detailed information about Mattermost services."""
    try:
        cmd = ["docker", "compose", "ps"]
        result = subprocess.run(
            cmd, cwd=MATTERMOST_DOCKER_DIR, capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get Mattermost services info: {e}")
        return f"Error: {e.stderr}"
    except Exception as e:
        logger.error(f"Unexpected error getting Mattermost services info: {e}")
        return f"Error: {str(e)}"


def is_mattermost_healthy():
    """Check if Mattermost is healthy and responding."""
    status = get_mattermost_backend_status()
    logger.info(f"Mattermost backend status: {status}")
    return status == "running"
