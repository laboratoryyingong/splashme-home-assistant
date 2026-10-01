"""SplashMe account entry: a one-time sign-in that authorises local devices.

The account entry stores the OAuth token and nothing else: no entities, no
polling. It is used to (1) learn which devices the account is paired with
and (2) fetch those devices' protocol v2 envelope keys. Paired devices that
answer on the local network are added as LAN entries automatically; devices
the account is not paired with cannot be added at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
import time

from aiohttp import ClientError, ClientResponseError

from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    LocalOAuth2ImplementationWithPkce,
    OAuth2Session,
    async_get_config_entry_implementation,
    async_register_implementation,
)
from homeassistant.helpers.device_registry import format_mac

from .api import AsyncConfigEntryAuth, SplashMeApiClient, SplashMeUserInfo, is_auth_failure
from .const import (
    CONF_CONNECTION,
    CONF_DEVICE_ID,
    CONF_ENVELOPE_KEY,
    CONF_MAC,
    CONNECTION_ACCOUNT,
    CONNECTION_LAN,
    DOMAIN,
    LAN_PORT,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_TOKEN,
)
from .lan import LanError, SplashMeLanClient, lan_hostname

_LOGGER = logging.getLogger(__name__)
_OAUTH_REGISTERED = f"{DOMAIN}_oauth_registered"


def async_register_builtin_oauth(hass: HomeAssistant) -> None:
    """Register the integration's own OAuth client (once per HA run).

    Adding the integration then goes straight to the SplashMe sign-in page
    instead of asking for application credentials. Called from async_setup
    and from the config flow, because with no entry yet only the flow runs.
    """
    if hass.data.get(_OAUTH_REGISTERED):
        return
    hass.data[_OAUTH_REGISTERED] = True
    async_register_implementation(
        hass,
        DOMAIN,
        LocalOAuth2ImplementationWithPkce(
            hass, DOMAIN, OAUTH2_CLIENT_ID, OAUTH2_AUTHORIZE, OAUTH2_TOKEN
        ),
    )


@dataclass(slots=True)
class SplashMeAccountRuntimeData:
    """Runtime data stored on an account config entry."""

    api: SplashMeApiClient
    oauth_session: OAuth2Session
    user_info: SplashMeUserInfo
    paired_device_ids: frozenset[str]


class NotPairedError(Exception):
    """The account is not paired with the device."""


def device_id_to_mac(device_id: str) -> str:
    """Cloud device id (A1_B2_C3_D4_E5_F6) -> registry MAC (a1:b2:c3:d4:e5:f6)."""
    return format_mac(device_id.replace("_", ":"))


def is_account_entry(entry: ConfigEntry) -> bool:
    """Return whether the entry is an account (auth-only) entry."""
    return entry.data.get(CONF_CONNECTION) == CONNECTION_ACCOUNT


def async_get_account(hass: HomeAssistant) -> SplashMeAccountRuntimeData | None:
    """Return the loaded account entry's runtime data, if any."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if is_account_entry(entry) and isinstance(
            getattr(entry, "runtime_data", None), SplashMeAccountRuntimeData
        ):
            return entry.runtime_data
    return None


async def async_paired_device_ids(api: SplashMeApiClient) -> frozenset[str]:
    """Return every device id the account can see across its sites."""
    ids: set[str] = set()
    for site in await api.async_get_user_sites():
        for device in site.get("device_list") or []:
            if device_id := device.get("deviceId"):
                ids.add(str(device_id))
    return frozenset(ids)


async def async_fetch_device_key(
    api: SplashMeApiClient, paired: frozenset[str], device_id: str
) -> str:
    """Return the device's envelope key; NotPairedError if the account may not."""
    if device_id not in paired:
        raise NotPairedError(device_id)
    try:
        return await api.async_get_envelope_key(device_id)
    except ClientResponseError as err:
        if err.status in (403, 404):
            raise NotPairedError(device_id) from err
        raise


async def async_setup_account_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Validate the token, cache the paired devices, then look for them on the LAN."""
    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except (ImplementationUnavailableError, ValueError) as err:
        # Entry signed in through application credentials that no longer
        # exist: signing in again binds it to the built-in client.
        raise ConfigEntryAuthFailed("SplashMe sign-in must be renewed") from err

    session = OAuth2Session(hass, entry, implementation)
    api = SplashMeApiClient(
        aiohttp_client.async_get_clientsession(hass), AsyncConfigEntryAuth(session)
    )
    try:
        user_info = await api.async_get_user_info()
        paired = await async_paired_device_ids(api)
    except ClientResponseError as err:
        if is_auth_failure(err):
            raise ConfigEntryAuthFailed("SplashMe OAuth token is no longer valid") from err
        raise ConfigEntryNotReady("Failed to reach the SplashMe API") from err
    except ClientError as err:
        raise ConfigEntryNotReady("Failed to reach the SplashMe API") from err

    entry.runtime_data = SplashMeAccountRuntimeData(
        api=api, oauth_session=session, user_info=user_info, paired_device_ids=paired
    )
    entry.async_create_background_task(
        hass, async_add_paired_lan_devices(hass, entry), "splashme_lan_matching"
    )
    return True


@dataclass(slots=True)
class FoundDevice:
    """A paired device that answered on the local network with a working key."""

    device_id: str
    mac: str
    host: str
    key_hex: str


LAN_SCAN_NOTIFICATION_ID = "splashme_lan_scan"


async def async_find_paired_lan_devices(
    hass: HomeAssistant,
    api: SplashMeApiClient,
    paired: frozenset[str],
    skip_macs: set[str],
) -> tuple[list[FoundDevice], list[str]]:
    """Look for every paired device on the local network, all at once.

    Returns the devices that answered and verified their key, and the ids of
    the paired devices that did not. Probes run in parallel so the scan takes
    one mDNS timeout (~5 s), not one per device.
    """
    session = aiohttp_client.async_get_clientsession(hass)
    candidates = sorted(d for d in paired if device_id_to_mac(d) not in skip_macs)

    async def probe(device_id: str) -> FoundDevice | None:
        mac = device_id_to_mac(device_id)
        try:
            host = await hass.async_add_executor_job(socket.gethostbyname, lan_hostname(mac))
            await SplashMeLanClient(session, host).async_probe()
        except (OSError, ClientError, TimeoutError, LanError):
            _LOGGER.debug("Paired device %s not found on the local network", device_id)
            return None
        try:
            key_hex = await async_fetch_device_key(api, paired, device_id)
            await SplashMeLanClient(session, host, key=bytes.fromhex(key_hex)).async_state()
        except (NotPairedError, ClientError, TimeoutError, LanError, ValueError) as err:
            _LOGGER.warning("Could not authorise local device %s: %s", device_id, err)
            return None
        return FoundDevice(device_id=device_id, mac=mac, host=host, key_hex=key_hex)

    started = time.monotonic()
    results = await asyncio.gather(*(probe(d) for d in candidates))
    found = [r for r in results if r is not None]
    missing = [d for d, r in zip(candidates, results, strict=True) if r is None]
    _LOGGER.info(
        "Local network scan: %d of %d paired devices found in %.1f s",
        len(found), len(candidates), time.monotonic() - started,
    )
    return found, missing


async def async_import_lan_device(hass: HomeAssistant, device: FoundDevice) -> None:
    """Create the LAN entry for a device the scan found."""
    _LOGGER.info("Adding paired device %s found at %s", device.device_id, device.host)
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_CONNECTION: CONNECTION_LAN,
            CONF_HOST: device.host,
            CONF_PORT: LAN_PORT,
            CONF_MAC: device.mac,
            CONF_DEVICE_ID: device.device_id,
            CONF_ENVELOPE_KEY: device.key_hex,
        },
    )


def configured_lan_macs(hass: HomeAssistant) -> set[str]:
    """Return the MACs of the LAN entries that already exist."""
    return {e.unique_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id}


async def async_add_paired_lan_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Add every paired device that answers on the local network.

    Runs in the background after the account entry loads (Home Assistant
    start, entry reload). A notification shows while the scan runs and, when
    a paired device stays unreachable, tells the user what to check.
    """
    account: SplashMeAccountRuntimeData = entry.runtime_data
    skip = configured_lan_macs(hass)
    pending = [d for d in account.paired_device_ids if device_id_to_mac(d) not in skip]
    if not pending:
        return
    persistent_notification.async_create(
        hass,
        f"Scanning the local network for {len(pending)} paired SplashMe device(s)…",
        title="SplashMe",
        notification_id=LAN_SCAN_NOTIFICATION_ID,
    )
    try:
        found, missing = await async_find_paired_lan_devices(
            hass, account.api, account.paired_device_ids, skip
        )
        for device in found:
            await async_import_lan_device(hass, device)
    finally:
        persistent_notification.async_dismiss(hass, LAN_SCAN_NOTIFICATION_ID)
    if missing:
        persistent_notification.async_create(
            hass,
            f"Added {len(found)} SplashMe device(s) found on the local network. "
            f"Not found: {', '.join(missing)}. Check that each device is powered and on the "
            "same network as Home Assistant; the scan runs again when Home Assistant "
            "restarts or the SplashMe account entry is reloaded.",
            title="SplashMe",
            notification_id=LAN_SCAN_NOTIFICATION_ID,
        )
