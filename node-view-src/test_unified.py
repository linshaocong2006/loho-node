import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import build_unified
import unified_core as core


class UnifiedTests(unittest.TestCase):
    def test_reject_nginx_and_command_injection_before_render(self):
        for domain in ('https://example.com', 'example.com;return 200', '*.example.com',
                       '../example.com', '127.0.0.1', 'example.com\ninclude /tmp/x;', '-evil.example', 'a..com'):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                core.validate_domain(domain)
        self.assertEqual(core.validate_domain('NODE.Example.com.'), 'node.example.com')
        for port in ('443;echo x', '22', '80', '33149', '65536', '0'):
            with self.assertRaises(ValueError):
                core.validate_port(port)

    def test_fresh_payload_all_features_and_no_old_server(self):
        files = build_unified.payload()
        core.verify_files(files)
        self.assertIn('RETENTION = 7 * 86400', files['history.py'])
        capabilities=[ast.literal_eval(value) for node in ast.walk(ast.parse(files['manager.py'])) if isinstance(node,ast.Dict) for key,value in zip(node.keys,node.values) if isinstance(key,ast.Constant) and key.value=='actions']
        self.assertTrue(any({'add','rename','delete','disable','enable'}<=set(actions) for actions in capabilities))
        for route in ('location = /node-view/api/snapshot.json', 'location ^~ /node-view/history/',
                      'location ^~ /node-view/manager/api/', 'location ^~ /node-view/'):
            self.assertIn(route, files['_nginx_snippet'])
        self.assertEqual(files['_nginx_snippet'].count('auth_basic_user_file'), 4)

    def test_custom_domain_port_applied_to_credential_and_origin(self):
        files = build_unified.payload()
        settings = {'domain': 'node.example.net', 'address': 'node.example.net', 'port': 5443}
        namespace = {'__name__': 'test_helpers'}
        with patch('pathlib.Path.read_text', return_value=json.dumps(settings)):
            exec(compile(files['manage_users.py'], 'manage_users.py', 'exec'), namespace)
        link = namespace['link']({'id': '11a02983-362a-4f61-92fe-8ed5a5745814'}, 'ADMIN', '/random/')
        parsed = urlsplit(link)
        self.assertEqual(parsed.hostname, settings['domain'])
        self.assertEqual(parsed.port, settings['port'])
        query = parse_qs(parsed.query)
        self.assertEqual(query['sni'], [settings['domain']])
        self.assertEqual(query['host'], [settings['domain']])
        self.assertEqual(query['path'], ['/random/'])
        self.assertEqual(core.origin(settings), 'https://node.example.net:5443')
        self.assertEqual(core.origin(dict(settings, port=443)), 'https://node.example.net')

    def test_corrupt_binary_is_never_written_or_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            filename = core.ASSETS['x86_64'][0]
            def download(args, **kwargs):
                (work / filename).write_bytes(b'corrupt archive')
                return ''
            with patch.object(core.platform, 'machine', return_value='x86_64'), \
                 patch.object(core, 'run', side_effect=download), patch.object(core, 'write') as write:
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    core.download_xray(work)
                write.assert_not_called()

    def test_rollback_restores_default_and_removes_only_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def mapped(value):
                path = Path(value)
                return root / str(path).lstrip('/') if path.is_absolute() else path
            constants = {name: mapped(getattr(core, name)) for name in ('APP', 'CONF', 'SITE', 'ENABLED', 'SNIPPET', 'REPORT')}
            other = mapped('/etc/nginx/sites-available/unrelated.conf')
            other.parent.mkdir(parents=True)
            other.write_text('keep me')
            for name in ('SITE', 'SNIPPET', 'REPORT'):
                constants[name].parent.mkdir(parents=True, exist_ok=True)
                constants[name].write_text('new')
            constants['APP'].mkdir(parents=True)
            constants['ENABLED'].parent.mkdir(parents=True)
            constants['ENABLED'].symlink_to(constants['SITE'])
            with patch.multiple(core, **constants), patch.object(core, 'Path', side_effect=mapped), \
                 patch.object(core.subprocess, 'run') as command, patch.object(core, 'log'):
                recovery = core.Rollback()
                recovery.armed = True
                recovery.created_user = True
                recovery.default_target = '/etc/nginx/sites-available/default'
                recovery.rollback()
                self.assertFalse(constants['SITE'].exists())
                self.assertFalse(constants['APP'].exists())
                self.assertEqual(other.read_text(), 'keep me')
                self.assertEqual(mapped('/etc/nginx/sites-enabled/default').readlink(), Path(recovery.default_target))
                self.assertTrue(any(c.args[0] == ['userdel', 'xray'] for c in command.call_args_list))

    def test_failed_https_auth_protection_fails_install_health(self):
        with patch.object(core, 'request', return_value=(200, b'{}')), patch.object(core.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, '认证保护'):
                core.health({'domain': 'node.example.net', 'port': 443}, 'loho', 'password')


if __name__ == '__main__':
    unittest.main()
