"""Regression checks: deploy packaging must not modify the existing UI."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class DeploymentPackageTests(unittest.TestCase):
    def test_dockerfile_exists_and_keeps_host_port(self):
        dockerfile = (ROOT / 'Dockerfile').read_text('utf-8')
        self.assertIn('FROM python:3.12-slim-bookworm', dockerfile)
        self.assertIn('EXPOSE 8765', dockerfile)
        self.assertIn('CMD ["python", "app_server.py"]', dockerfile)

    def test_secrets_excluded_from_docker_context(self):
        ignored = (ROOT / '.dockerignore').read_text('utf-8').splitlines()
        self.assertIn('.env', ignored)
        self.assertIn('data_cache/*.csv', ignored)
        self.assertIn('data_cache/*.json', ignored)

    def test_no_render_specific_config(self):
        self.assertFalse((ROOT / 'render.yaml').exists())

    def test_market_server_and_ui_files_exist(self):
        for rel in ['index.html','styles.css','src/app.js','src/analytics.js','assets/logo.png','app_server.py','market_api.py','groq_client.py']:
            with self.subTest(file=rel):
                self.assertTrue((ROOT / rel).is_file())

if __name__ == '__main__': unittest.main()
