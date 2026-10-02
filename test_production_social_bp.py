"""
Production single-entry app: social blueprint registration (fix F1).

The production process runs ``bot.create_mini_app()`` (Waitress single
entry), while ``serve_miniapp.py`` is the standalone development server.
Before this fix only ``serve_miniapp.py`` registered ``social_bp``, so
every ``/api/social/*`` URL answered 404 in production.

Verifies:
- ``bot.py`` registers ``social_bp`` inside ``create_mini_app()``.
- The production app's URL map contains all four social routes.
- ``GET /api/social/accounts`` answers 401 (unauthenticated), NOT 404 —
  proof the route is actually routed in the production app.
- Existing blueprints (tasks, withdrawal, deposit) stay registered.
"""

import unittest

from bot import create_mini_app


class TestSocialBlueprintRegistrationSource:
    """bot.py must import and register social_bp inside create_mini_app()."""

    def test_bot_source_registers_social_bp(self):
        with open("bot.py", "r", encoding="utf-8") as f:
            source = f.read()
        assert "from social_routes import social_bp" in source, (
            "bot.py must import social_bp from social_routes"
        )
        assert "register_blueprint(social_bp)" in source, (
            "bot.py must register social_bp on the production mini app"
        )

    def test_registration_is_inside_create_mini_app(self):
        with open("bot.py", "r", encoding="utf-8") as f:
            source = f.read()
        start = source.index("def create_mini_app()")
        end = source.index("def create_mini_app_server")
        body = source[start:end]
        assert "register_blueprint(social_bp)" in body, (
            "social_bp must be registered inside create_mini_app(), "
            "not (only) elsewhere"
        )


class TestProductionUrlMap(unittest.TestCase):
    """The production Flask app must expose every social route."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_mini_app()
        cls.rules = {rule.rule for rule in cls.app.url_map.iter_rules()}

    def test_social_routes_present(self):
        expected = {
            "/api/social/youtube/connect",
            "/api/social/youtube/callback",
            "/api/social/accounts",
            "/api/social/youtube/unlink",
        }
        missing = expected - self.rules
        assert not missing, f"social routes missing from production app: {sorted(missing)}"

    def test_existing_blueprints_still_registered(self):
        # Regression guard: the F1 fix must not drop the other blueprints.
        expected = {
            "/api/tasks",
            "/api/withdrawal/methods",
            "/api/deposit/methods",
        }
        missing = expected - self.rules
        assert not missing, f"pre-existing routes missing: {sorted(missing)}"

    def test_miniapp_index_still_served(self):
        client = self.app.test_client()
        response = client.get("/")
        assert response.status_code == 200


class TestSocialRouteServesRequests(unittest.TestCase):
    """Unauthenticated calls must reach the handler (401), not 404."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_mini_app()

    def test_accounts_unauthenticated_returns_401_not_404(self):
        client = self.app.test_client()
        response = client.get("/api/social/accounts")
        assert response.status_code == 401, (
            f"expected 401 unauthenticated, got {response.status_code} — "
            "social_bp is not registered in the production app"
        )
        payload = response.get_json()
        assert payload == {"ok": False, "error": "unauthenticated"}


if __name__ == "__main__":
    unittest.main()
