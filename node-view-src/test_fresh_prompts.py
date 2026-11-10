"""Exercise the extracted installer without changing the host."""
import importlib.util
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fresh_installer', Path(sys.argv.pop(1))/'installer.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PromptTests(unittest.TestCase):
    def test_invalid_fields_repeat_only_the_current_prompt(self):
        def resolve(domain, *args, **kwargs):
            if domain == 'bad.example.net':
                raise socket.gaierror(socket.EAI_NONAME, 'missing')
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',80))]

        def free(port):
            if port == 5001:
                raise ValueError('port in use')

        inputs = iter([
            'bad.example.net','node.example.net',  # DNS failure
            'bad','5001','',                       # Invalid, occupied, default panel port
            'bad-email','', 'bad name','',        # Email and username
            '5000','',                            # REALITY port conflicts with panel
            'https://www.apple.com','node.example.net','',
            'maybe','',
        ])
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return next(inputs)

        passwords = ['short','long-password-1','does-not-match','long-password-2','long-password-2']
        with patch.object(installer.sys.stdin,'isatty',return_value=True), patch('builtins.input',side_effect=answer), \
             patch.object(installer.socket,'getaddrinfo',side_effect=resolve), patch.object(installer,'free_port',side_effect=free), \
             patch.object(installer.getpass,'getpass',side_effect=passwords), patch.object(installer,'log'):
            settings,email,username,password,bbr = installer.settings_prompt()

        self.assertEqual(prompts[0],prompts[1])
        self.assertEqual(prompts[2],prompts[3])
        self.assertEqual(prompts[3],prompts[4])
        self.assertEqual(prompts[9],prompts[10])
        self.assertEqual(prompts[11:14],[prompts[11]]*3)
        self.assertEqual((settings['domain'],settings['port'],settings['reality']['port']),
                         ('node.example.net',5000,443))
        self.assertEqual(settings['reality']['target'],'www.apple.com:443')
        self.assertEqual((email,username,password,bbr),('','loho','long-password-2',False))

        inbound, = installer.xray_config(settings,[],'test-key')['inbounds']
        self.assertEqual(inbound['port'],443)
        self.assertEqual(inbound['streamSettings']['security'],'reality')
        self.assertEqual(inbound['streamSettings']['xhttpSettings']['mode'],'packet-up')
        nginx = installer.nginx_config(settings)
        self.assertIn('listen 5000 ssl;',nginx)
        self.assertIn('/.well-known/acme-challenge/',nginx)
        self.assertNotIn(settings['reality']['path'],nginx)
        self.assertNotIn('listen 443',nginx)

    def test_reality_network_failure_keeps_other_settings(self):
        settings={'domain':'node.example.net','port':5000,
                  'reality':{'sni':'bad.example.net','target':'bad.example.net:443','port':443,'short_id':'abc123'}}
        with patch('builtins.input',return_value='www.apple.com') as prompt, patch.object(installer,'log'), \
             patch.object(installer,'check_reality_target',side_effect=[ValueError('unreachable'),None]) as check:
            installer.verify_reality_settings(settings)
        self.assertEqual(check.call_args_list[0].args,('bad.example.net',))
        self.assertEqual(check.call_args_list[1].args,('www.apple.com',))
        self.assertEqual(prompt.call_count,1)
        self.assertEqual(settings['reality'],{'sni':'www.apple.com','target':'www.apple.com:443','port':443,'short_id':'abc123'})
        self.assertEqual(settings['port'],5000)

    def test_acme_port_reuses_only_the_distro_nginx(self):
        listeners = 'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=7,fd=4))'
        with patch.object(installer,'free_port',side_effect=ValueError('TCP80 in use')), \
             patch.object(Path,'is_symlink',return_value=True), \
             patch.object(Path,'resolve',return_value=Path('/etc/nginx/sites-available/default')), \
             patch.object(installer.shutil,'which',return_value='/usr/bin/ss'), patch.object(installer,'log'):
            with patch.object(installer,'run',side_effect=['7\n',listeners]):
                installer.check_acme_port()
            with patch.object(installer,'run',side_effect=['7\n',listeners.replace('nginx','apache2')]):
                with self.assertRaisesRegex(ValueError,'TCP80 in use'):
                    installer.check_acme_port()


if __name__ == '__main__':
    unittest.main()
