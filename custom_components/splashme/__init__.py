"""The SplashMe integration."""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from aiohttp import ClientError, ClientResponseError

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    aiohttp_client,
    device_registry as dr,
)
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    OAuth2Session,
    async_get_config_entry_implementation,
)

from .account import (
    SplashMeAccountRuntimeData,
    async_register_builtin_oauth,
    async_setup_account_entry,
    is_account_entry,
)
from .api import (
    AsyncConfigEntryAuth,
    SplashMeApiClient,
    SplashMeUserInfo,
    is_auth_failure,
)
from .const import (
    CONF_CONNECTION,
    CONF_ENVELOPE_KEY,
    CONNECTION_LAN,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    PLATFORMS_LAN,
)
from .dashboard import async_register_dashboard, async_remove_dashboard, async_track_renames
from .coordinator import SplashMeDataCoordinator, SplashMeDevice
from .lan import SplashMeLanClient, SplashMeLanCoordinator
from .slots import SplashMeLanSlotEntities, async_track_equipment


@dataclass(slots=True)
class SplashMeRuntimeData:
    """Runtime data stored on the config entry."""

    api: SplashMeApiClient
    oauth_session: OAuth2Session
    user_info: SplashMeUserInfo
    coordinator: SplashMeDataCoordinator


@dataclass(slots=True)
class SplashMeLanRuntimeData:
    """Runtime data stored on a LAN config entry."""

    client: SplashMeLanClient
    coordinator: SplashMeLanCoordinator
    slot_entities: list[SplashMeLanSlotEntities] = field(default_factory=list)


SplashMeConfigEntry = ConfigEntry[
    SplashMeRuntimeData | SplashMeLanRuntimeData | SplashMeAccountRuntimeData
]


def is_lan_entry(entry: SplashMeConfigEntry) -> bool:
    """Return whether the entry is a local (LAN) device entry."""
    return entry.data.get(CONF_CONNECTION) == CONNECTION_LAN


FRONTEND_URL_BASE = "/splashme-frontend"
_FRONTEND_FLAG = f"{DOMAIN}_frontend_registered"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled Lovelace cards (once per HA run)."""
    if hass.data.get(_FRONTEND_FLAG):
        return
    hass.data[_FRONTEND_FLAG] = True
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                FRONTEND_URL_BASE,
                str(Path(__file__).parent / "frontend"),
                cache_headers=False,
            )
        ]
    )
    # The static path sends no Cache-Control, so browsers cache the scripts
    # heuristically (hours). A content-hash query string makes every edit a
    # new URL and lets a plain reload pick it up.
    for name in ("splashme-tank-card.js", "splashme-heater-card.js", "splashme-status-cards.js"):
        card = Path(__file__).parent / "frontend" / name
        digest = await hass.async_add_executor_job(_file_digest, card)
        add_extra_js_url(hass, f"{FRONTEND_URL_BASE}/{name}?v={digest}")


def _file_digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def _device_model(device: SplashMeDevice) -> str:
    """Return the device model label shown in Home Assistant."""
    return f"Pool Controller ({'Online' if device.online else 'Offline'})"


def _sync_registered_devices(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    devices: tuple[SplashMeDevice, ...],
) -> None:
    """Sync SplashMe devices into the Home Assistant device registry."""
    device_registry = dr.async_get(hass)
    active_identifiers = {(DOMAIN, device.device_id) for device in devices}

    for device in devices:
        device_entry = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.device_id)},
            manufacturer=MANUFACTURER,
            name=device.display_name,
            model=_device_model(device),
            serial_number=device.device_id,
        )
        device_registry.async_update_device(device_entry.id, area_id=None)

    for existing_device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        splashme_identifiers = {
            identifier
            for identifier in existing_device.identifiers
            if identifier[0] == DOMAIN
        }
        if splashme_identifiers and splashme_identifiers.isdisjoint(active_identifiers):
            device_registry.async_remove_device(existing_device.id)


async def _async_setup_lan_entry(
    hass: HomeAssistant, entry: SplashMeConfigEntry
) -> bool:
    """Set up a local (LAN) device entry."""
    from .services import async_register_services

    await async_register_services(hass)

    key_hex = entry.data.get(CONF_ENVELOPE_KEY)
    client = SplashMeLanClient(
        aiohttp_client.async_get_clientsession(hass),
        entry.data[CONF_HOST],
        key=bytes.fromhex(key_hex) if key_hex else None,
    )
    coordinator = SplashMeLanCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SplashMeLanRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LAN)
    await async_register_dashboard(hass, entry, coordinator)
    async_track_renames(hass, entry, coordinator)
    async_track_equipment(hass, entry, coordinator)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SplashMeConfigEntry) -> None:
    """Delete what the entry created outside the entity registry: its dashboard."""
    if is_lan_entry(entry):
        await async_remove_dashboard(hass, entry)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the frontend card during bootstrap, before config entries.

    Browsers reconnecting after an HA restart reload the page as soon as the
    websocket is up; registering here (rather than only in async_setup_entry)
    makes the card script part of the served index from the start.
    """
    del config
    await _async_register_frontend(hass)
    async_register_builtin_oauth(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SplashMeConfigEntry) -> bool:
    """Set up SplashMe from a config entry."""
    await _async_register_frontend(hass)

    if is_lan_entry(entry):
        return await _async_setup_lan_entry(hass, entry)
    if is_account_entry(entry):
        return await async_setup_account_entry(hass, entry)

    from .services import async_register_services

    await async_register_services(hass)

    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except ImplementationUnavailableError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="oauth2_implementation_unavailable",
        ) from err

    session = OAuth2Session(hass, entry, implementation)
    client = SplashMeApiClient(
        aiohttp_client.async_get_clientsession(hass),
        AsyncConfigEntryAuth(session),
    )

    try:
        user_info = await client.async_get_user_info()
    except ClientResponseError as err:
        if is_auth_failure(err):
            raise ConfigEntryAuthFailed(
                "SplashMe OAuth token is no longer valid"
            ) from err
        raise ConfigEntryNotReady("Failed to fetch SplashMe user info") from err
    except ClientError as err:
        raise ConfigEntryNotReady("Failed to reach the SplashMe API") from err

    coordinator = SplashMeDataCoordinator(hass, client, user_info, entry)
    await coordinator.async_config_entry_first_refresh()
    _sync_registered_devices(hass, entry, coordinator.data.devices)

    entry.runtime_data = SplashMeRuntimeData(
        api=client,
        oauth_session=session,
        user_info=user_info,
        coordinator=coordinator,
    )
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _sync_registered_devices(hass, entry, coordinator.data.devices)
        )
    )

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SplashMeConfigEntry) -> bool:
    """Unload a config entry."""
    if is_account_entry(entry):
        return True
    platforms = PLATFORMS_LAN if is_lan_entry(entry) else PLATFORMS
    if not platforms:
        return True
    return await hass.config_entries.async_unload_platforms(entry, platforms)
