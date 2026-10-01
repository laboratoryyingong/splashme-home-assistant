"""API helpers for the SplashMe integration."""

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientResponseError, ClientSession

from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    IOT_SHADOW_DEVICE_BASE,
    IOT_SHADOW_V2_DEVICE_BASE,
    OAUTH2_USERINFO,
    USER_DEVICE_ONLINE_STATUSES,
    USER_SITES,
)


@dataclass(frozen=True, slots=True)
class SplashMeUserInfo:
    """Authenticated SplashMe account details."""

    sub: str
    email: str | None
    preferred_username: str | None
    given_name: str | None
    family_name: str | None
    name: str | None
    role: str | None

    @property
    def display_name(self) -> str:
        """Return the preferred label for the config entry."""
        return self.email or self.preferred_username or self.name or "SplashMe"


@dataclass(frozen=True, slots=True)
class StaticTokenAuth:
    """Use a fixed access token."""

    access_token: str

    async def async_get_access_token(self) -> str:
        """Return the configured access token."""
        return self.access_token


class AsyncConfigEntryAuth:
    """Provide SplashMe authentication tied to an OAuth2 config entry."""

    def __init__(
        self,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        """Initialize SplashMe auth."""
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid access token."""
        await self._oauth_session.async_ensure_token_valid()
        return self._oauth_session.token[CONF_ACCESS_TOKEN]


class SplashMeApiClient:
    """Minimal SplashMe API client for OAuth-backed requests."""

    def __init__(
        self,
        websession: ClientSession,
        auth: StaticTokenAuth | AsyncConfigEntryAuth,
    ) -> None:
        """Initialize the API client."""
        self._websession = websession
        self._auth = auth

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated API request."""
        access_token = await self._auth.async_get_access_token()
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {access_token}"},
            "raise_for_status": True,
        }
        if json_data is not None:
            kwargs["json"] = json_data

        async with self._websession.request(method, url, **kwargs) as response:
            return await response.json()

    async def _async_get_data(self, url: str) -> dict[str, Any]:
        """Perform a GET request and return the wrapped data payload."""
        payload = await self._async_request("GET", url)
        return dict(payload.get("data") or {})

    async def async_get_envelope_key(self, device_id: str) -> str:
        """Return the device's protocol v2 envelope key (hex, 16 bytes).

        The backend only serves it to an account paired with the device.
        """
        data = await self._async_get_data(f"{IOT_SHADOW_V2_DEVICE_BASE}/{device_id}/envelope-key")
        return str(data.get("key_hex") or "")

    async def async_get_user_info(self) -> SplashMeUserInfo:
        """Fetch the current authenticated user profile."""
        payload = await self._async_request("GET", OAUTH2_USERINFO)

        return SplashMeUserInfo(
            sub=str(payload["sub"]),
            email=payload.get("email"),
            preferred_username=payload.get("preferred_username"),
            given_name=payload.get("given_name"),
            family_name=payload.get("family_name"),
            name=payload.get("name"),
            role=payload.get("role"),
        )

    async def async_get_user_sites(self) -> list[dict[str, Any]]:
        """Fetch all accessible sites for the current user."""
        payload = await self._async_get_data(USER_SITES)
        return list(payload.get("sites") or [])

    async def async_get_device_online_statuses(self) -> dict[str, bool]:
        """Fetch online statuses for all visible devices."""
        payload = await self._async_get_data(USER_DEVICE_ONLINE_STATUSES)
        devices = payload.get("devices") or []
        return {
            str(device.get("deviceId") or ""): bool(device.get("online"))
            for device in devices
            if device.get("deviceId")
        }

    async def async_get_pump_info(self, device_id: str) -> dict[str, Any]:
        """Fetch pump information for a device."""
        payload = await self._async_get_data(f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/pumpInfo")
        return dict(payload.get("pump") or {})

    async def async_get_aux_info(self, device_id: str) -> dict[str, Any]:
        """Fetch aux information for a device."""
        payload = await self._async_get_data(f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/auxInfo")
        return dict(payload.get("manual") or {})

    async def async_get_chemistry_card(self, device_id: str) -> dict[str, Any]:
        """Fetch the full chemistry card (chemistry, phSettings, chlorineSettings)."""
        payload = await self._async_get_data(
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/chemistryCard"
        )
        return dict(payload or {})

    async def async_get_dashboard_info(self, device_id: str) -> dict[str, Any]:
        """Fetch dashboard telemetry (incl. live dosing relay states)."""
        payload = await self._async_get_data(
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/dashboardInfo"
        )
        return dict(payload.get("dashboard") or {})

    async def async_get_ambient_temp(self, device_id: str) -> dict[str, Any]:
        """Fetch ambient and water temperature information for a device."""
        payload = await self._async_get_data(f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/ambientTemp")
        return dict(payload.get("ambientTemp") or {})

    async def async_set_aux(
        self,
        device_id: str,
        *,
        aux_type_code: int,
        aux_slot_num: int,
        enabled: bool,
    ) -> None:
        """Toggle an aux device."""
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/manualSet",
            json_data={
                "aux_switch_state": 1 if enabled else 0,
                "aux_type_code": aux_type_code,
                "aux_slot_num": aux_slot_num,
            },
        )

    async def async_set_pump_info(
        self,
        device_id: str,
        *,
        aux_slot_num: int,
        enabled: bool,
        pump_speed: int,
    ) -> None:
        """Update the main pump state."""
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/setPumpInfo",
            json_data={
                "aux_switch_state": 1 if enabled else 0,
                "aux_type_code": 31,
                "aux_slot_num": aux_slot_num,
                "pump_speed": pump_speed,
            },
        )

    async def async_get_schedule_info(self, device_id: str) -> list[dict[str, Any]]:
        """Fetch the device's schedule list."""
        payload = await self._async_get_data(
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/scheduleInfo"
        )
        info = (payload or {}).get("scheduleInfo") or {}
        return list(info.get("schedules") or [])

    async def async_change_schedule_state(
        self, device_id: str, schedule_id: int, state: bool
    ) -> None:
        """Enable or disable a schedule."""
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/changeScheduleStatus",
            json_data={"schedule_id": schedule_id, "schedule_state": state},
        )

    async def async_create_schedule(self, device_id: str, body: dict[str, Any]) -> None:
        """Create a new schedule."""
        await self._async_request(
            "POST", f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/createSchedule", json_data=body
        )

    async def async_update_schedule(self, device_id: str, body: dict[str, Any]) -> None:
        """Update an existing schedule."""
        await self._async_request(
            "POST", f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/updateSchedule", json_data=body
        )

    async def async_delete_schedule(self, device_id: str, schedule_num: int) -> None:
        """Delete a schedule."""
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/deleteSchedule",
            json_data={"schedule_num": schedule_num},
        )

    async def async_set_ph_settings(
        self, device_id: str, settings: dict[str, Any]
    ) -> None:
        """Write the complete pH settings object.

        The firmware overwrites every field from the payload (missing fields
        become 0), so callers must always send the full settings object.
        """
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/setPhSettings",
            json_data=settings,
        )

    async def async_set_chlorine_settings(
        self, device_id: str, settings: dict[str, Any]
    ) -> None:
        """Write the complete chlorine/ORP settings object (see async_set_ph_settings)."""
        await self._async_request(
            "POST",
            f"{IOT_SHADOW_DEVICE_BASE}/{device_id}/setChlorineSettings",
            json_data=settings,
        )


def is_auth_failure(err: ClientResponseError) -> bool:
    """Return whether the HTTP error indicates invalid OAuth credentials."""
    return err.status in (400, 401, 403)
