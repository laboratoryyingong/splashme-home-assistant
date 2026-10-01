"""Config flow for SplashMe.

Two kinds of entries:

* account: a one-time OAuth sign-in. It creates no entities; it authorises
  local devices (only devices the account is paired with can be added) and
  adds the paired devices it finds on the local network.
* lan: one local device, discovered via mDNS (or added by the account
  entry). Its protocol v2 key comes from the account entry, or from a
  sign-in inside the device flow when no account entry exists yet.
"""

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any, override

from aiohttp import ClientError, ClientResponseError

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .account import (
    FoundDevice,
    NotPairedError,
    async_fetch_device_key,
    async_find_paired_lan_devices,
    async_get_account,
    async_register_builtin_oauth,
    async_import_lan_device,
    async_paired_device_ids,
    configured_lan_macs,
    device_id_to_mac,
)
from .api import SplashMeApiClient, StaticTokenAuth, is_auth_failure
from .const import (
    CONF_CONNECTION,
    CONF_DEVICE_ID,
    CONF_ENVELOPE_KEY,
    CONF_MAC,
    CONNECTION_ACCOUNT,
    CONNECTION_LAN,
    DOMAIN,
    LAN_PORT,
    OAUTH2_SCOPES,
)
from .lan import LanError, SplashMeLanClient

_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow: account sign-in and local (LAN) devices."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._lan_discovery: dict[str, Any] = {}
        # Account sign-in: kept between the OAuth step, the scan and finish.
        self._account_data: dict[str, Any] = {}
        self._account_title = ""
        self._scan_api: SplashMeApiClient | None = None
        self._paired: frozenset[str] = frozenset()
        self._scan_task: asyncio.Task[tuple[list[FoundDevice], list[str]]] | None = None
        self._found: list[FoundDevice] = []
        self._missing: list[str] = []

    @property
    @override
    def logger(self) -> logging.Logger:
        """Return logger."""
        return logging.getLogger(__name__)

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Adding the integration means signing in; devices come from discovery."""
        if self.source == SOURCE_REAUTH:
            return await super().async_step_user(user_input)
        return await self.async_step_account()

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the OAuth sign-in with the built-in client."""
        async_register_builtin_oauth(self.hass)
        return await self.async_step_pick_implementation()

    # -- LAN: discovered device ---------------------------------------------

    def _lan_client(self, key_hex: str | None = None) -> SplashMeLanClient:
        return SplashMeLanClient(
            aiohttp_client.async_get_clientsession(self.hass),
            self._lan_discovery[CONF_HOST],
            key=bytes.fromhex(key_hex) if key_hex else None,
            port=self._lan_discovery.get(CONF_PORT, LAN_PORT),
        )

    async def _async_probe_lan(self) -> bool:
        """Return whether the device answers on its pv2 bridge."""
        try:
            await self._lan_client().async_probe()
        except (ClientError, TimeoutError, LanError):
            return False
        return True

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device discovered via mDNS.

        Identity comes from the TXT `mac` record only — the instance name can
        be renamed by mDNS reflectors and the IP changes with DHCP. With an
        account entry present, devices the account is not paired with are
        not offered at all.
        """
        mac = discovery_info.properties.get("mac")
        if not mac:
            return self.async_abort(reason="no_mac")

        await self.async_set_unique_id(device_id_to_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.host})

        device_id = discovery_info.properties.get("id")
        account = async_get_account(self.hass)
        if account is not None and device_id not in account.paired_device_ids:
            return self.async_abort(reason="not_paired")

        self._lan_discovery = {
            CONF_CONNECTION: CONNECTION_LAN,
            CONF_HOST: discovery_info.host,
            CONF_PORT: discovery_info.port or LAN_PORT,
            CONF_MAC: self.unique_id,
            CONF_DEVICE_ID: device_id,
        }
        name = device_id or discovery_info.name.split(".")[0]
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the discovered device."""
        if user_input is not None:
            if not await self._async_probe_lan():
                return self.async_abort(reason="cannot_connect")
            return await self._async_authorize_lan_device()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self.context.get("title_placeholders", {}).get("name", ""),
                "host": self._lan_discovery[CONF_HOST],
            },
        )

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create a LAN entry for a paired device the account entry found."""
        await self.async_set_unique_id(data[CONF_MAC])
        self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
        return self.async_create_entry(title=data[CONF_DEVICE_ID], data=data)

    # -- LAN: obtain the envelope key ---------------------------------------

    async def _async_authorize_lan_device(self) -> ConfigFlowResult:
        """Fetch the key through the account entry, or ask to sign in."""
        account = async_get_account(self.hass)
        if account is None:
            # No account yet: sign in inside this device flow.
            return await self.async_step_account()
        device_id = self._lan_discovery.get(CONF_DEVICE_ID)
        try:
            key_hex = await async_fetch_device_key(
                account.api, account.paired_device_ids, device_id or ""
            )
        except NotPairedError:
            return self.async_abort(reason="not_paired")
        except ClientError as err:
            self.logger.error("Failed to fetch the envelope key for %s: %s", device_id, err)
            return self.async_abort(reason="key_fetch_failed")
        return await self._async_finish_lan_entry(key_hex)

    async def _async_key_works(self, key_hex: str) -> bool:
        """Return whether a state frame verifies with the key."""
        try:
            await self._lan_client(key_hex).async_state()
        except (ClientError, TimeoutError, LanError, ValueError):
            return False
        return True

    async def _async_finish_lan_entry(self, key_hex: str) -> ConfigFlowResult:
        if not _KEY_RE.match(key_hex) or not await self._async_key_works(key_hex):
            return self.async_abort(reason="key_fetch_failed")
        return self._async_create_lan_entry(key_hex)

    def _async_create_lan_entry(self, key_hex: str) -> ConfigFlowResult:
        title = self._lan_discovery.get(CONF_DEVICE_ID) or f"SplashMe {self._lan_discovery[CONF_HOST]}"
        return self.async_create_entry(
            title=title, data={**self._lan_discovery, CONF_ENVELOPE_KEY: key_hex}
        )

    # -- OAuth completion ---------------------------------------------------

    @property
    @override
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data appended to the authorize URL."""
        return {"scope": " ".join(OAUTH2_SCOPES)}

    @override
    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Finish the sign-in: authorise a pending LAN device, or create the account entry."""
        client = SplashMeApiClient(
            aiohttp_client.async_get_clientsession(self.hass),
            StaticTokenAuth(data[CONF_TOKEN][CONF_ACCESS_TOKEN]),
        )

        try:
            user_info = await client.async_get_user_info()
            paired = await async_paired_device_ids(client)
        except ClientResponseError as err:
            if is_auth_failure(err):
                return self.async_abort(reason="oauth_unauthorized")
            self.logger.error("Failed to fetch SplashMe account details: %s", err)
            return self.async_abort(reason="oauth_failed")
        except ClientError as err:
            self.logger.error("Network error while fetching SplashMe account details: %s", err)
            return self.async_abort(reason="oauth_failed")

        if self._lan_discovery:
            # Sign-in inside a device flow: the token is used once for the
            # key and discarded; the entry never talks to the cloud again.
            device_id = self._lan_discovery.get(CONF_DEVICE_ID) or ""
            try:
                key_hex = await async_fetch_device_key(client, paired, device_id)
            except NotPairedError:
                return self.async_abort(reason="not_paired")
            except ClientError as err:
                self.logger.error("Failed to fetch the envelope key for %s: %s", device_id, err)
                return self.async_abort(reason="key_fetch_failed")
            return await self._async_finish_lan_entry(key_hex)

        await self.async_set_unique_id(user_info.sub)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)

        self._abort_if_unique_id_configured()
        # Scan the local network before creating the entry, so the user sees
        # progress and the result instead of a silent background task.
        self._account_data = {**data, CONF_CONNECTION: CONNECTION_ACCOUNT}
        self._account_title = user_info.display_name
        self._scan_api = client
        self._paired = paired
        return await self.async_step_scan()

    # -- Account: local network scan -----------------------------------------

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Look for the paired devices on the local network, showing progress."""
        if self._scan_task is None:
            assert self._scan_api is not None
            self._scan_task = self.hass.async_create_task(
                async_find_paired_lan_devices(
                    self.hass, self._scan_api, self._paired, configured_lan_macs(self.hass)
                )
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan",
                progress_action="scan_lan",
                progress_task=self._scan_task,
                description_placeholders={"paired_count": str(len(self._paired))},
            )
        if (err := self._scan_task.exception()) is not None:
            self.logger.error("Local network scan failed: %s", err)
            self._found, self._missing = [], sorted(self._paired)
        else:
            self._found, self._missing = self._scan_task.result()
        return self.async_show_progress_done(next_step_id="scan_done")

    async def async_step_scan_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what the scan found and let the user finish or scan again."""
        configured = len(self._paired) - len(self._found) - len(self._missing)
        return self.async_show_menu(
            step_id="scan_done",
            menu_options=["finish", "rescan"],
            description_placeholders={
                "found_count": str(len(self._found) + configured),
                "paired_count": str(len(self._paired)),
                "found": ", ".join(f"{d.device_id} ({d.host})" for d in self._found) or "–",
                "missing": ", ".join(self._missing) or "–",
            },
        )

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the scan again."""
        self._scan_task = None
        return await self.async_step_scan()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the devices found and create the account entry."""
        for device in self._found:
            await async_import_lan_device(self.hass, device)
        return self.async_create_entry(title=self._account_title, data=self._account_data)

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauthentication upon an API auth error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the reauthentication dialog."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()
