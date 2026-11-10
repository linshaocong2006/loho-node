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
            'bad name','',                        # Invalid, then default username
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
            settings,username,password,bbr = installer.settings_prompt()

        self.assertEqual(prompts[0],prompts[1])
        self.assertEqual(prompts[2],prompts[3])
        self.assertEqual(prompts[3],prompts[4])
        self.assertEqual(prompts[5:7],['网站登录用户名 [loho]：']*2)
        self.assertEqual(prompts[7],prompts[8])
        self.assertEqual(prompts[9:12],[prompts[9]]*3)
        self.assertEqual((settings['domain'],settings['port'],settings['reality']['port']),
                         ('node.example.net',5000,3000))
        self.assertEqual(settings['reality']['target'],'www.apple.com:443')
        self.assertEqual((username,password,bbr),('loho','long-password-2',False))

        inbound, = installer.xray_config(settings,[],'test-key')['inbounds']
        self.assertEqual(inbound['port'],3000)
        self.assertEqual(inbound['streamSettings']['security'],'reality')
        self.assertEqual(inbound['streamSettings']['xhttpSettings']['mode'],'packet-up')
        nginx = installer.nginx_config(settings)
        self.assertIn('listen 5000 ssl;',nginx)
        self.assertIn('/.well-known/acme-challenge/',nginx)
        self.assertNotIn(settings['reality']['path'],nginx)
        self.assertNotIn('listen 3000',nginx)

    def test_default_reality_port_avoids_panel_on_3000(self):
        answers = ['node.example.net','3000','','','','']
        with patch.object(installer.sys.stdin,'isatty',return_value=True), \
             patch('builtins.input',side_effect=answers), \
             patch.object(installer,'node_domain',return_value=('node.example.net',['1.1.1.1'])), \
             patch.object(installer,'free_port'), patch.object(installer,'website_password'), \
             patch.object(installer,'log'):
            settings,*_ = installer.settings_prompt()
        self.assertEqual((settings['port'],settings['reality']['port']),(3000,3001))

    def test_install_only_starts_after_confirmation_of_edited_settings(self):
        answers = iter([
            'node.example.net','', 'admin-test','', '', 'y',
            '', '9', '2',
            '', '3000', '', '', '', '',
            '1',
        ])
        with patch.object(installer.sys.stdin,'isatty',return_value=True), \
             patch.object(installer,'preflight'), patch.object(installer,'free_port'), \
             patch.object(installer,'node_domain',side_effect=lambda domain:(domain,['1.1.1.1'])), \
             patch.object(installer.getpass,'getpass',side_effect=['saved-password','saved-password','']) as password, \
             patch.object(installer,'log') as log, patch.object(installer,'LOG',None), \
             patch.object(installer,'write') as write, patch.object(installer,'Installation'), \
             patch.object(installer,'run',side_effect=RuntimeError('stop at apt')) as run:
            def answer(prompt):
                write.assert_not_called()
                run.assert_not_called()
                return next(answers)

            with patch('builtins.input',side_effect=answer):
                with self.assertRaisesRegex(RuntimeError,'stop at apt'):
                    installer.install({})

        run.assert_called_once_with(['apt-get','-o','DPkg::Lock::Timeout=120','update'])
        messages = [call.args[0] for call in log.call_args_list]
        self.assertIn('管理后台：https://node.example.net:3000/node-view/',messages)
        self.assertIn('REALITY 节点：node.example.net:3001（VLESS + XHTTP packet-up，初始用户 ADMIN）',messages)
        self.assertIn('后台用户名：admin-test',messages)
        self.assertIn('启用 BBR：是',messages)
        self.assertIn('回车保留当前设置。',messages)
        self.assertNotIn('saved-password','\n'.join(messages))
        self.assertEqual(password.call_count,3)

    def test_exit_from_confirmation_does_not_start_installation(self):
        with patch.object(installer,'preflight'), \
             patch.object(installer,'settings_prompt',return_value=({},'loho','test-password',False)), \
             patch('builtins.input',return_value='3'), patch.object(installer,'log'), \
             patch.object(installer,'write') as write, patch.object(installer,'run') as run:
            installer.install({})
        write.assert_not_called()
        run.assert_not_called()

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
