"""Offline session-key regressions using Flask's in-memory test client."""
import unittest
from unittest.mock import patch

from web.app import create_app


class SessionSecretTests(unittest.TestCase):
    @staticmethod
    def _app():
        return create_app(None, None, None, None, None)

    @staticmethod
    def _read_session_after_restart(first, second):
        client = first.test_client()
        with client.session_transaction() as session:
            session['test_value'] = 'retained'
        cookie_name = first.config['SESSION_COOKIE_NAME']
        cookie = client.get_cookie(cookie_name)
        restarted_client = second.test_client()
        restarted_client.set_cookie(cookie_name, cookie.value)
        with restarted_client.session_transaction() as session:
            return session.get('test_value')

    def test_configured_secret_preserves_sessions_without_warning(self):
        with patch.dict('os.environ', {'SECRET_KEY': 'configured-test-secret'}, clear=True):
            with patch('web.app.logger') as logger:
                first = self._app()
                second = self._app()
        self.assertEqual(first.secret_key, 'configured-test-secret')
        self.assertEqual(second.secret_key, 'configured-test-secret')
        logger.warning.assert_not_called()
        self.assertEqual(self._read_session_after_restart(first, second), 'retained')

    def _assert_random_fallback(self, environment):
        with patch.dict('os.environ', environment, clear=True):
            with patch('web.app.logger') as logger:
                first = self._app()
                second = self._app()
        self.assertIsInstance(first.secret_key, str)
        self.assertGreaterEqual(len(first.secret_key), 64)
        self.assertIsInstance(second.secret_key, str)
        self.assertGreaterEqual(len(second.secret_key), 64)
        self.assertTrue(first.secret_key != second.secret_key)
        self.assertEqual(logger.warning.call_count, 2)
        for call in logger.warning.call_args_list:
            message = call.args[0]
            self.assertIn('SECRET_KEY', message)
            self.assertIn('restart', message)
            self.assertNotIn(first.secret_key, message)
            self.assertNotIn(second.secret_key, message)
        self.assertIsNone(self._read_session_after_restart(first, second))

    def test_missing_secret_generates_fresh_key_and_warning(self):
        self._assert_random_fallback({})

    def test_empty_secret_generates_fresh_key_and_warning(self):
        self._assert_random_fallback({'SECRET_KEY': ''})


if __name__ == '__main__':
    unittest.main()
