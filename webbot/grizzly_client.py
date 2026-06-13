"""
grizzly_client.py — Python client for Grizzly SMS (SMS-Activate-compatible handler_api.php)
Base: https://api.grizzlysms.com/stubs/handler_api.php
Auth: ?api_key=<key>
"""
import requests

BASE_URL = "https://api.grizzlysms.com/stubs/handler_api.php"

ERROR_MESSAGES = {
    "NO_KEY": "Invalid or missing API key",
    "BAD_KEY": "Invalid API key",
    "ERROR_SQL": "Server error, try again",
    "BAD_ACTION": "Invalid action",
    "BAD_SERVICE": "Invalid service code",
    "NO_NUMBERS": "No numbers available right now for this service/country",
    "NO_BALANCE": "Insufficient balance on provider account",
    "WRONG_SERVICE": "Invalid service",
    "BAD_STATUS": "Invalid status",
    "NO_ACTIVATION": "Activation not found or expired",
    "SQL_ERROR": "Server error, try again",
}


class GrizzlyError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, f"Grizzly error: {code}")
        super().__init__(self.message)


class GrizzlyClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()

    def _get(self, params):
        params = dict(params)
        params["api_key"] = self.api_key
        try:
            resp = self.session.get(BASE_URL, params=params, timeout=20)
        except requests.RequestException as e:
            raise GrizzlyError("NO_CONNECTION", str(e))

        ctype = resp.headers.get("content-type", "")
        text = resp.text.strip()

        if "application/json" in ctype:
            try:
                return resp.json()
            except ValueError:
                pass

        # plain text responses
        if text in ("NO_KEY", "BAD_KEY", "USERS_IP_IS_NOT_ALLOWED", "SERVICE_UNAVAILABLE_REGION"):
            raise GrizzlyError(text)
        if text.startswith(("BAD_", "ERROR_", "WRONG_")):
            raise GrizzlyError(text)

        return text

    # ── Balance ─────────────────────────────────────────────
    def get_balance(self):
        resp = self._get({"action": "getBalance"})
        if isinstance(resp, str) and resp.startswith("ACCESS_BALANCE:"):
            return float(resp.split(":")[1])
        raise GrizzlyError("PARSE_ERROR", f"Unexpected balance response: {resp}")

    # ── Services / Prices ──────────────────────────────────
    def get_services_list(self):
        """Returns list of {code, name} via getServicesList."""
        resp = self._get({"action": "getServicesList"})
        if isinstance(resp, dict):
            services = resp.get("services") or resp.get("data") or []
            out = []
            for s in services:
                code = s.get("code") or s.get("service") or s.get("id")
                name = s.get("name") or s.get("title") or code
                if code:
                    out.append({"code": code, "name": name})
            return out
        raise GrizzlyError("PARSE_ERROR", "Unexpected services response")

    def get_prices_v2(self, country=None, service=None):
        """
        getPricesV2 -> {service: {country: {cost, count}}}
        """
        params = {"action": "getPricesV2"}
        if country is not None:
            params["country"] = country
        if service is not None:
            params["service"] = service
        resp = self._get(params)
        if isinstance(resp, dict):
            return resp
        raise GrizzlyError("PARSE_ERROR", "Unexpected getPricesV2 response")

    def get_countries(self):
        resp = self._get({"action": "getCountries"})
        if isinstance(resp, (dict, list)):
            return resp
        raise GrizzlyError("PARSE_ERROR", "Unexpected countries response")

    # ── Activation lifecycle ───────────────────────────────
    def get_number(self, service, country="0", max_price=None, operator=None):
        params = {"action": "getNumber", "service": service, "country": country}
        if max_price is not None:
            params["maxPrice"] = max_price
        if operator:
            params["operator"] = operator
        resp = self._get(params)
        if isinstance(resp, str) and resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return {"activation_id": parts[1], "phone": parts[2]}
        raise GrizzlyError(resp if isinstance(resp, str) else "UNKNOWN", f"getNumber failed: {resp}")

    def get_status(self, activation_id):
        resp = self._get({"action": "getStatus", "id": activation_id})
        if not isinstance(resp, str):
            return {"status": "WAIT"}
        if resp.startswith("STATUS_OK:"):
            return {"status": "OK", "code": resp.split(":", 1)[1]}
        if resp.startswith("STATUS_WAIT_RETRY:"):
            return {"status": "WAIT_RETRY", "code": resp.split(":", 1)[1]}
        if resp in ("STATUS_WAIT_CODE", "STATUS_WAIT_RESEND"):
            return {"status": "WAIT"}
        if resp == "STATUS_CANCEL":
            return {"status": "CANCEL"}
        if resp == "NO_ACTIVATION":
            raise GrizzlyError("NO_ACTIVATION")
        return {"status": "WAIT", "raw": resp}

    def set_status(self, activation_id, status_code):
        """
        status=1 -> number received (ready)
        status=3 -> request another code
        status=6 -> complete activation
        status=8 -> cancel activation
        """
        resp = self._get({"action": "setStatus", "id": activation_id, "status": status_code})
        return resp
