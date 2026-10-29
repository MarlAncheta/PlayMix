import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    app_module.limiter.enabled = False  # tests that need rate limiting turn it back on
    app_module.limiter.reset()
    return app_module.app.test_client()
