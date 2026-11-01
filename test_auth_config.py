import os
from pathlib import Path
import tempfile
import unittest
import auth_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.env'

    def test_file_pair_overrides_stale_environment_and_other_env_wins(self):
        self.path.write_text('PINTUANYA_COOKIE="sid=fresh"\nPINTUANYA_DEVICE_ID=device\nPINTUANYA_GROUP=file\n')
        env = {'PINTUANYA_COOKIE': 'sid=old', 'PINTUANYA_DEVICE_ID': 'old-device', 'PINTUANYA_GROUP': 'env'}
        cfg = auth_config.AuthConfig(self.path, env)
        cfg.load()
        self.assertEqual(env['PINTUANYA_COOKIE'], 'sid=fresh')
        self.assertEqual(env['PINTUANYA_DEVICE_ID'], 'device')
        self.assertEqual(env['PINTUANYA_GROUP'], 'env')

    def test_file_missing_credentials_does_not_fall_back_or_mix(self):
        self.path.write_text('PINTUANYA_GROUP=test\n')
        env = {'PINTUANYA_COOKIE': 'sid=old', 'PINTUANYA_DEVICE_ID': 'old'}
        auth_config.AuthConfig(self.path, env).load()
        self.assertNotIn('PINTUANYA_COOKIE', env)
        self.assertNotIn('PINTUANYA_DEVICE_ID', env)

    def test_parser_never_executes_or_expands_shell(self):
        values = auth_config.parse_env('export PINTUANYA_COOKIE=\'sid=$(touch /tmp/no); x=$HOME\'\nA=foo # comment\n')
        self.assertEqual(values['PINTUANYA_COOKIE'], 'sid=$(touch /tmp/no); x=$HOME')
        self.assertEqual(values['A'], 'foo')
        with self.assertRaisesRegex(ValueError, '2') as caught:
            auth_config.parse_env('A=ok\nSECRET-VALUE\n')
        self.assertNotIn('SECRET-VALUE', str(caught.exception))

    def test_save_preserves_comments_settings_and_uses_private_permissions(self):
        self.path.write_text('# hello\nPINTUANYA_GROUP=group\nPINTUANYA_COOKIE=old\n')
        cfg = auth_config.AuthConfig(self.path, {})
        cfg.load()
        cfg.save('sid=new; x="quoted"', 'device')
        values = auth_config.parse_env(self.path.read_text())
        self.assertEqual(values['PINTUANYA_COOKIE'], 'sid=new; x="quoted"')
        self.assertEqual(values['PINTUANYA_DEVICE_ID'], 'device')
        self.assertEqual(values['PINTUANYA_GROUP'], 'group')
        self.assertIn('# hello', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cfg.environ['PINTUANYA_COOKIE'], 'sid=new; x="quoted"')

    def test_conflict_preserves_other_process_update(self):
        self.path.write_text('PINTUANYA_COOKIE=sid=old\n')
        cfg = auth_config.AuthConfig(self.path, {})
        cfg.load()
        self.path.write_text('PINTUANYA_COOKIE=sid=other\n')
        with self.assertRaisesRegex(ValueError, '修改'):
            cfg.save('sid=new', 'device')
        self.assertIn('sid=other', self.path.read_text())

    def test_symlink_rejected_without_reading_or_writing_target(self):
        target = self.path.with_name('target')
        target.write_text('private')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            auth_config.AuthConfig(self.path, {}).load()
        self.assertEqual(target.read_text(), 'private')

    def test_disabled_file_uses_external_environment_and_cannot_save(self):
        env = {'PINTUANYA_COOKIE': 'sid=external'}
        cfg = auth_config.AuthConfig(None, env)
        cfg.load()
        self.assertEqual(env['PINTUANYA_COOKIE'], 'sid=external')
        with self.assertRaises(ValueError):
            cfg.save('sid=new', 'device')

    def test_server_cookie_deletion_can_clear_persisted_cookie(self):
        self.path.write_text('PINTUANYA_COOKIE=sid=old\nPINTUANYA_DEVICE_ID=device\n')
        cfg = auth_config.AuthConfig(self.path, {})
        cfg.load()
        cfg.save('', 'device')
        self.assertEqual(auth_config.parse_env(self.path.read_text())['PINTUANYA_COOKIE'], '')
