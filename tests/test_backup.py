import io
import json
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backup_clp.__main__ import main
from backup_clp.acervo import Acervo, ErroAcervo, sha256
from backup_clp.config import Config, ConfigFTP, ConfigMaquina, ErroConfig, carregar, interpretar
from backup_clp.demo import criar_demo
from backup_clp.diferenca import comparar, comparar_bytes, decodificar, em_texto, regioes_binarias
from backup_clp.exportar import exportar_git
from backup_clp.remoto import Coletor, baixar_ftp
from backup_clp.servidor import criar_servidor
from backup_clp.vigia import Vigia, casa

RAIZ = Path(__file__).resolve().parent.parent


class Relogio:
    """Relógio de mentira: os testes andam no tempo sem esperar."""

    def __init__(self, inicio):
        self.t = inicio

    def __call__(self):
        return self.t


def zip_bytes(entradas):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, dados in entradas.items():
            z.writestr(nome, dados)
    return buf.getvalue()


class ComPasta(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-clp-teste-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- acervo

class TestAcervo(ComPasta):
    def test_deduplicacao_por_conteudo(self):
        with Acervo(self.tmp / "a") as a:
            v1 = a.registrar("Extrusora 1", "prog.wps", b"programa 1")
            self.assertIsNone(a.registrar("Extrusora 1", "prog.wps", b"programa 1"), "mesmo conteúdo não é versão nova")
            v2 = a.registrar("Extrusora 1", "prog.wps", b"programa 2")
            v3 = a.registrar("Extrusora 1", "prog.wps", b"programa 1")      # voltou ao antigo: versão nova
            a.registrar("Injetora 2", "copia.wps", b"programa 1")          # mesmo conteúdo em outra máquina
            self.assertEqual([v.id for v in a.versoes("Extrusora 1")], [v1.id, v2.id, v3.id])
            self.assertEqual(v1.hash, v3.hash)
            self.assertEqual(len(list((self.tmp / "a" / "objetos").glob("*/*"))), 2, "cada conteúdo guardado uma vez")
            self.assertEqual(a.ler(v1.hash), b"programa 1")
            self.assertEqual(v1.hash, sha256(b"programa 1"))

    def test_conteudo_corrompido_e_detectado(self):
        with Acervo(self.tmp / "a") as a:
            v = a.registrar("M", "x.bin", b"original")
            obj = self.tmp / "a" / "objetos" / v.hash[:2] / v.hash[2:]
            obj.write_bytes(b"adulterado")
            with self.assertRaisesRegex(ErroAcervo, "corrompido"):
                a.ler(v.hash)
            with self.assertRaises(ErroAcervo):
                a.versao(999)

    def test_retencao_por_quantidade_e_idade(self):
        rel = Relogio(datetime(2026, 1, 1, 8))
        with Acervo(self.tmp / "a", relogio=rel) as a:
            for i in range(6):
                rel.t = datetime(2026, 1, 1, 8) + timedelta(days=10 * i)
                a.registrar("M", "p.wps", f"versão {i}".encode())
            a.registrar("M", "unico.csv", b"so uma")      # 51 dias atrás no fim, mas é a mais nova dele
            rel.t = datetime(2026, 3, 1)
            apagadas, objetos = a.aplicar_retencao(versoes=4)
            self.assertEqual((apagadas, objetos), (2, 2))
            restantes = a.versoes("M", "p.wps")
            self.assertEqual([a.ler(v.hash).decode() for v in restantes], [f"versão {i}" for i in range(2, 6)])
            # 20 dias antes de 01/03 = 09/02: saem as de 21/01 e 31/01; a de 10/02 fica.
            apagadas, _ = a.aplicar_retencao(dias=20)
            self.assertEqual(apagadas, 2)
            self.assertEqual(len(a.versoes("M", "p.wps")), 2)
            rel.t = datetime(2027, 1, 1)
            a.aplicar_retencao(dias=20)
            self.assertEqual(len(a.versoes("M", "p.wps")), 1, "a mais nova nunca sai, por mais velha que seja")
            self.assertEqual(len(a.versoes("M", "unico.csv")), 1)
            self.assertEqual(len(list((self.tmp / "a" / "objetos").glob("*/*"))), 2)


# ---------------------------------------------------------------- vigia

class TestVigia(ComPasta):
    def preparar(self, espera=10):
        self.pasta = self.tmp / "projetos"
        self.pasta.mkdir()
        self.cfg = Config(espera_estavel=espera, maquinas=[
            ConfigMaquina("Extrusora 1", pastas=[str(self.pasta)], padroes=["*.wps", "*.V6F"])])
        self.rel = Relogio(1_800_000_000.0)
        self.acervo = Acervo(self.tmp / "acervo")
        self.addCleanup(self.acervo.fechar)
        self.vigia = Vigia(self.cfg, self.acervo, relogio=self.rel)

    def gravar(self, nome, dados, idade):
        p = self.pasta / nome
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(dados)
        os.utime(p, (self.rel.t - idade, self.rel.t - idade))
        return p

    def test_arquivo_antigo_entra_na_primeira_varredura_e_so_uma_vez(self):
        self.preparar()
        self.gravar("prog.wps", b"programa", idade=3600)
        self.gravar("sub/receita.V6F", b"\x00\x01binario", idade=3600)
        self.gravar("leia-me.txt", b"fora do padrao", idade=3600)
        self.gravar("~prog.wps", b"temporario do editor", idade=3600)
        novas = self.vigia.varrer()
        self.assertEqual(sorted(c.arquivo for c in novas), ["prog.wps", "sub/receita.V6F"])
        self.assertEqual(self.vigia.varrer(), [])
        v = novas[0].versao
        self.assertIn(str(self.pasta), v.origem)
        self.assertTrue(v.modificado)

    def test_arquivo_instavel_nao_e_copiado(self):
        self.preparar(espera=10)
        p = self.gravar("prog.wps", b"metade do", idade=0)          # acabou de ser gravado
        self.assertEqual(self.vigia.varrer(), [], "recém-gravado não entra")
        self.rel.t += 5
        p.write_bytes(b"metade do salvamento")                      # ainda gravando
        os.utime(p, (self.rel.t, self.rel.t))
        self.assertEqual(self.vigia.varrer(), [])
        self.rel.t += 6                                             # parado há 6 s < 10 s
        self.assertEqual(self.vigia.varrer(), [])
        self.rel.t += 5                                             # parado há 11 s
        novas = self.vigia.varrer()
        self.assertEqual(len(novas), 1)
        self.assertEqual(self.acervo.ler(novas[0].versao.hash), b"metade do salvamento")
        self.assertEqual(len(self.acervo.versoes()), 1, "a versão pela metade nunca entrou")

    def test_copia_que_preserva_mtime_antigo_espera_estabilizar(self):
        self.preparar(espera=10)
        p = self.gravar("prog.wps", b"v1", idade=3600)
        self.assertEqual(len(self.vigia.varrer()), 1)
        p.write_bytes(b"v2 sendo copiada")
        os.utime(p, (self.rel.t - 7200, self.rel.t - 7200))          # mtime antigo, conteúdo novo
        self.rel.t += 1
        self.assertEqual(self.vigia.varrer(), [], "mudou agora: espera mesmo com mtime antigo")
        self.rel.t += 11
        self.assertEqual(len(self.vigia.varrer()), 1)

    def test_resalvar_igual_nao_gera_versao(self):
        self.preparar(espera=0)
        p = self.gravar("prog.wps", b"igual", idade=100)
        self.assertEqual(len(self.vigia.varrer()), 1)
        os.utime(p, (self.rel.t - 10, self.rel.t - 10))            # salvou de novo sem mudar nada
        self.assertEqual(self.vigia.varrer(), [])
        self.assertEqual(len(self.acervo.versoes()), 1)

    def test_pasta_inacessivel_nao_derruba(self):
        self.preparar()
        self.cfg.maquinas[0].pastas.insert(0, str(self.tmp / "nao-existe"))
        self.gravar("prog.wps", b"x", idade=3600)
        novas = self.vigia.varrer()
        self.assertEqual([c.arquivo for c in novas], ["projetos/prog.wps"], "várias pastas: prefixo com o nome dela")

    def test_padroes_sem_diferenciar_maiusculas(self):
        self.assertTrue(casa("PROG.v6f", ["*.V6F"]))
        self.assertTrue(casa("~$prog.wps", ["~*"]))
        self.assertFalse(casa("prog.wps.bak", ["*.wps"]))


# ---------------------------------------------------------------- diferenças

class TestDiferenca(unittest.TestCase):
    def test_diff_texto_unificado(self):
        a = b"receita;temp\nPEAD;180\nPP;200\n"
        b = b"receita;temp\nPEAD;185\nPP;200\nPS;210\n"
        r = comparar_bytes(a, b, "a.csv", "b.csv")
        self.assertEqual(r["tipo"], "texto")
        self.assertEqual((r["adicionadas"], r["removidas"]), (2, 1))
        self.assertIn("-PEAD;180", r["linhas"])
        self.assertIn("+PEAD;185", r["linhas"])
        self.assertIn("+PS;210", r["linhas"])
        self.assertTrue(r["linhas"][0].startswith("--- a.csv"))

    def test_xml_de_uma_linha_so_quebra_entre_tags(self):
        # Setup Configuration.xsc do WPS 2.60: tudo numa linha; a mudança de IP tem que aparecer sozinha
        campos = "".join(f"<lCampo{i}>{i}</lCampo{i}>" for i in range(30))
        a = f'<?xml version="1.0"?><setup>{campos}<lIPAddress>192.168.000.010</lIPAddress><lDHCP>1</lDHCP></setup>'
        b = a.replace("192.168.000.010", "192.168.003.040").replace("<lDHCP>1", "<lDHCP>0")
        r = comparar_bytes(a.encode(), b.encode(), "a.xsc", "b.xsc")
        self.assertEqual((r["adicionadas"], r["removidas"]), (2, 2))
        self.assertIn("+<lIPAddress>192.168.003.040</lIPAddress>", r["linhas"])
        self.assertIn("-<lDHCP>1</lDHCP>", r["linhas"])

    def test_texto_utf16_e_latin1(self):
        self.assertEqual(decodificar("tag;descrição\n".encode("utf-16")), "tag;descrição\n")
        self.assertEqual(decodificar("pressão".encode("latin-1")), "pressão")
        self.assertIsNone(decodificar(b"\x00\x01\x02\x03binario"))

    def test_diff_zip(self):
        a = zip_bytes({"projeto.xml": "<p v='1'/>\n", "telas/t1.xml": "<tela>\n<botao/>\n</tela>\n",
                       "logo.png": b"\x89PNG\x00\x00\x01", "velho.csv": "x\n"})
        b = zip_bytes({"projeto.xml": "<p v='1'/>\n", "telas/t1.xml": "<tela>\n<botao/>\n<lampada/>\n</tela>\n",
                       "logo.png": b"\x89PNG\x00\x00\x02", "telas/t2.xml": "<tela/>\n"})
        r = comparar_bytes(a, b)
        self.assertEqual(r["tipo"], "zip")
        self.assertEqual(r["adicionadas"], ["telas/t2.xml"])
        self.assertEqual(r["removidas"], ["velho.csv"])
        self.assertEqual(r["iguais"], 1)
        alt = {e["nome"]: e for e in r["alteradas"]}
        self.assertEqual(alt["telas/t1.xml"]["tipo"], "texto")
        self.assertIn("+<lampada/>", alt["telas/t1.xml"]["linhas"])
        self.assertEqual(alt["logo.png"]["tipo"], "binario")
        self.assertEqual(alt["logo.png"]["regioes"], [{"inicio": 6, "fim": 7}])
        texto = "\n".join(em_texto(r))
        self.assertIn("+ telas/t2.xml", texto)
        self.assertIn("- velho.csv", texto)

    def test_zip_quebrado_cai_para_binario(self):
        a = b"PK\x03\x04" + b"lixo" * 20
        b = b"PK\x03\x04" + b"lixa" * 20
        self.assertEqual(comparar_bytes(a, b)["tipo"], "binario")

    def test_diff_binario_regioes(self):
        a = bytes(range(256)) * 64                     # 16 KiB
        b = bytearray(a)
        b[100:104] = b"\xff" * 4
        b[110] ^= 1                                    # perto da anterior: junta numa região só
        b[5000] ^= 1
        b[9000:9010] = b"\x00" * 10
        b += b"\x00" * 30                              # cresceu
        r = comparar_bytes(a, bytes(b))
        self.assertEqual(r["tipo"], "binario")
        self.assertEqual([(g["inicio"], g["fim"]) for g in r["regioes"]],
                         [(100, 111), (5000, 5001), (9000, 9010), (16384, 16414)])
        self.assertEqual((r["tamanho_a"], r["tamanho_b"]), (16384, 16414))
        self.assertNotEqual(r["hash_a"], r["hash_b"])
        self.assertIn("0x00001388", "\n".join(em_texto(r)))   # 5000

    def test_regioes_binario_encolheu_e_igual(self):
        self.assertEqual(regioes_binarias(b"abcdef", b"abc"), [[3, 6]])
        self.assertEqual(comparar_bytes(b"\x00\x01", b"\x00\x01")["tipo"], "igual")


# ---------------------------------------------------------------- FTP falso

class FTPFalso(socketserver.ThreadingTCPServer):
    """Servidor FTP mínimo (USER, PASS, TYPE, PASV, RETR, NLST, QUIT) só para os testes."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, arquivos, usuario="backup", senha="segredo"):
        self.arquivos, self.usuario, self.senha = arquivos, usuario, senha
        self.retr = []
        super().__init__(("127.0.0.1", 0), FTPHandler)


class FTPHandler(socketserver.StreamRequestHandler):
    def responder(self, texto):
        self.wfile.write((texto + "\r\n").encode())

    def handle(self):
        srv, pasv, logado, user = self.server, None, False, None
        self.responder("220 IHM de mentira")
        try:
            for linha in self.rfile:
                cmd, _, arg = linha.decode().strip().partition(" ")
                cmd = cmd.upper()
                if cmd == "USER":
                    user = arg
                    self.responder("331 senha")
                elif cmd == "PASS":
                    logado = user == srv.usuario and arg == srv.senha
                    self.responder("230 ok" if logado else "530 login incorreto")
                elif not logado:
                    self.responder("530 faça login")
                elif cmd == "TYPE":
                    self.responder("200 ok")
                elif cmd == "PASV":
                    pasv = socket.create_server(("127.0.0.1", 0))
                    porta = pasv.getsockname()[1]
                    self.responder(f"227 Entering Passive Mode (127,0,0,1,{porta >> 8},{porta & 255})")
                elif cmd in ("RETR", "NLST"):
                    if cmd == "RETR" and arg not in srv.arquivos:
                        self.responder("550 não existe")
                        continue
                    if cmd == "RETR":
                        srv.retr.append(arg)
                        dados = srv.arquivos[arg]
                    else:
                        base = arg.rstrip("/") + "/"
                        nomes = [n for n in srv.arquivos if n.startswith(base) and "/" not in n[len(base):]]
                        dados = "".join(n + "\r\n" for n in nomes).encode()
                    self.responder("150 abrindo")
                    conn, _ = pasv.accept()
                    with conn:
                        conn.sendall(dados)
                    pasv.close()
                    pasv = None
                    self.responder("226 ok")
                elif cmd == "QUIT":
                    self.responder("221 tchau")
                    return
                else:
                    self.responder("502 não implementado")
        finally:
            if pasv:
                pasv.close()


class ComFTP(ComPasta):
    def subir_ftp(self, arquivos):
        srv = FTPFalso(arquivos)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv


class TestFTP(ComFTP):
    def test_baixa_arquivo_e_pasta(self):
        srv = self.subir_ftp({"/project/ihm.cmtp": b"PROJ1", "/backup/a.emtp": b"A", "/backup/b.log": b"L"})
        c = ConfigFTP("127.0.0.1", srv.server_address[1], "backup", "segredo", ["/project/ihm.cmtp", "/backup/"])
        itens = baixar_ftp(c, ["*.emtp", "*.cmtp"])
        self.assertEqual(itens, [("/project/ihm.cmtp", b"PROJ1"), ("/backup/a.emtp", b"A")])

    def test_coletor_guarda_so_quando_muda_e_respeita_intervalo(self):
        arquivos = {"/project/ihm.cmtp": b"PROJETO v1"}
        srv = self.subir_ftp(arquivos)
        ftp = ConfigFTP("127.0.0.1", srv.server_address[1], "backup", "segredo", ["/project/ihm.cmtp"], intervalo=60)
        cfg = Config(maquinas=[ConfigMaquina("Injetora 2", ftp=ftp, autor="ihm")])
        rel = Relogio(1000.0)
        with Acervo(self.tmp / "a") as a:
            col = Coletor(cfg, a, relogio=rel)
            novas = col.coletar()
            self.assertEqual(len(novas), 1)
            v = novas[0].versao
            self.assertEqual(v.arquivo, "ftp/127.0.0.1/project/ihm.cmtp")
            self.assertTrue(v.origem.startswith("ftp://127.0.0.1:"))
            self.assertEqual(v.autor, "ihm")
            arquivos["/project/ihm.cmtp"] = b"PROJETO v2"
            rel.t += 30
            self.assertEqual(col.coletar(), [], "antes do intervalo não vai ao FTP")
            self.assertEqual(len(srv.retr), 1)
            rel.t += 31
            self.assertEqual(len(col.coletar()), 1)
            rel.t += 61
            self.assertEqual(col.coletar(), [], "igual ao último: não é versão nova")
            self.assertEqual(len(a.versoes("Injetora 2")), 2)

    def test_ihm_fora_do_ar_e_senha_errada_viram_erro_sem_derrubar(self):
        srv = self.subir_ftp({"/p.cmtp": b"x"})
        livre = socket.create_server(("127.0.0.1", 0))
        porta_morta = livre.getsockname()[1]
        livre.close()
        cfg = Config(maquinas=[
            ConfigMaquina("Desligada", ftp=ConfigFTP("127.0.0.1", porta_morta, caminhos=["/p.cmtp"], timeout=2)),
            ConfigMaquina("Senha errada", ftp=ConfigFTP("127.0.0.1", srv.server_address[1], "backup", "errada",
                                                       ["/p.cmtp"], timeout=2)),
            ConfigMaquina("Boa", ftp=ConfigFTP("127.0.0.1", srv.server_address[1], "backup", "segredo", ["/p.cmtp"]))])
        with Acervo(self.tmp / "a") as a:
            col = Coletor(cfg, a)
            novas = col.coletar()
            self.assertEqual([c.maquina for c in novas], ["Boa"])
            self.assertEqual(set(col.erros), {("Desligada", "ftp"), ("Senha errada", "ftp")})


class TestHTTP(ComPasta):
    def test_coleta_http(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                corpo = b"backup do programa"
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/backup/programa.bin"
        cfg = interpretar({"maquina": [{"nome": "Sopradora 3", "http": {"urls": [url]}}]}, self.tmp)
        with Acervo(self.tmp / "a") as a:
            novas = Coletor(cfg, a).coletar()
            self.assertEqual([c.arquivo for c in novas], ["http/127.0.0.1/backup/programa.bin"])
            self.assertEqual(a.ler(novas[0].versao.hash), b"backup do programa")


# ---------------------------------------------------------------- linha de comando e restauração

def rodar(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        codigo = main(list(args))
    return codigo, out.getvalue(), err.getvalue()


class TestCLI(ComPasta):
    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "projetos"
        self.proj.mkdir()
        self.toml = self.tmp / "backup-clp.toml"
        self.toml.write_text(f'acervo = "acervo"\nespera_estavel = 0\n[[maquina]]\nnome = "Extrusora 1"\n'
                             f'pastas = ["projetos"]\npadroes = ["*.csv", "*.wps"]\n', encoding="utf-8")

    def salvar(self, nome, dados):
        p = self.proj / nome
        p.write_bytes(dados)
        os.utime(p, (p.stat().st_mtime - 60,) * 2)

    def test_vigiar_versoes_diff_restaurar(self):
        c = str(self.toml)
        self.salvar("receitas.csv", b"receita;temp\nPEAD;180\n")
        codigo, out, _ = rodar("-c", c, "vigiar", "--uma-vez")
        self.assertEqual(codigo, 0)
        self.assertIn("nova versão #1 de receitas.csv", out)
        self.salvar("receitas.csv", b"receita;temp\nPEAD;185\n")
        rodar("-c", c, "vigiar", "--uma-vez")

        codigo, out, _ = rodar("-c", c, "versoes", "extrusora 1")
        self.assertEqual(codigo, 0)
        self.assertEqual(len(out.strip().splitlines()), 3)
        codigo, out, _ = rodar("-c", c, "versoes", "Extrusora 1", "--json")
        self.assertEqual([v["id"] for v in json.loads(out)], [1, 2])

        codigo, out, _ = rodar("-c", c, "diff", "1", "2")
        self.assertIn("-PEAD;180", out)
        self.assertIn("+PEAD;185", out)
        codigo, out2, _ = rodar("-c", c, "diff", "2")                  # só B: compara com a anterior
        self.assertEqual(out, out2)
        self.assertEqual(rodar("-c", c, "diff", "1")[0], 1)

        # Restaurar: o arquivo da máquina foi estragado e a versão 1 volta byte a byte.
        alvo = self.tmp / "restaurado" / "receitas.csv"
        codigo, out, _ = rodar("-c", c, "restaurar", "1", str(alvo))
        self.assertEqual(codigo, 0)
        self.assertEqual(alvo.read_bytes(), b"receita;temp\nPEAD;180\n")
        codigo, _, err = rodar("-c", c, "restaurar", "2", str(alvo))
        self.assertEqual(codigo, 1)
        self.assertIn("--forcar", err)
        self.assertEqual(alvo.read_bytes(), b"receita;temp\nPEAD;180\n", "não sobrescreve sem --forcar")
        rodar("-c", c, "restaurar", "2", str(alvo), "--forcar")
        self.assertEqual(alvo.read_bytes(), b"receita;temp\nPEAD;185\n")
        rodar("-c", c, "restaurar", "1", str(self.tmp / "restaurado"), "--forcar")   # pasta: usa o nome original
        self.assertEqual((self.tmp / "restaurado" / "receitas.csv").read_bytes(), b"receita;temp\nPEAD;180\n")
        self.assertFalse(list((self.tmp / "restaurado").glob("*.restaurando")))

    def test_restaurar_recusa_conteudo_corrompido(self):
        self.salvar("prog.wps", b"programa bom")
        rodar("-c", str(self.toml), "vigiar", "--uma-vez")
        for obj in (self.tmp / "acervo" / "objetos").glob("*/*"):
            obj.write_bytes(b"estragado")
        alvo = self.tmp / "saida.wps"
        codigo, _, err = rodar("-c", str(self.toml), "restaurar", "1", str(alvo))
        self.assertEqual(codigo, 2)
        self.assertIn("corrompido", err)
        self.assertFalse(alvo.exists())

    def test_retencao_pela_cli(self):
        for i in range(5):
            self.salvar("prog.wps", f"v{i}".encode())
            rodar("-c", str(self.toml), "vigiar", "--uma-vez")
        codigo, out, _ = rodar("-c", str(self.toml), "retencao", "--versoes", "2")
        self.assertIn("3 versão(ões)", out)
        self.assertEqual(json.loads(rodar("-c", str(self.toml), "versoes", "Extrusora 1", "--json")[1])[0]["id"], 4)

    def test_erros_de_configuracao(self):
        self.assertEqual(rodar("-c", str(self.tmp / "nao.toml"), "verificar")[0], 2)
        with self.assertRaisesRegex(ErroConfig, "nenhuma"):
            interpretar({})
        with self.assertRaisesRegex(ErroConfig, "pastas"):
            interpretar({"maquina": [{"nome": "X"}]})
        with self.assertRaisesRegex(ErroConfig, "repetida"):
            interpretar({"maquina": [{"nome": "X", "pastas": "a"}, {"nome": "x", "pastas": "b"}]})
        with self.assertRaisesRegex(ErroConfig, "caminhos"):
            interpretar({"maquina": [{"nome": "X", "ftp": {"host": "10.0.0.9"}}]})

    def test_verificar_avisa_quando_nada_casa(self):
        proj = self.tmp / "WPS 2.60 Configurations" / "OP3495" / "ladderdiagram"
        proj.mkdir(parents=True)
        (proj / "02_Extrusora.xdd").write_text("<ladder/>")
        toml = self.tmp / "wps.toml"
        pasta = (self.tmp / "WPS 2.60 Configurations").as_posix()
        toml.write_text(f'[[maquina]]\nnome = "Extrusora"\npastas = ["{pasta}"]\n')
        codigo, out, _ = rodar("-c", str(toml), "verificar")
        self.assertEqual(codigo, 0)
        self.assertIn("0 arquivo(s) casam", out)
        self.assertIn('padroes = ["*"]', out)
        toml.write_text(f'[[maquina]]\nnome = "Extrusora"\npastas = ["{pasta}"]\npadroes = ["*"]\n')
        out = rodar("-c", str(toml), "verificar")[1]
        self.assertIn("1 arquivo(s) casam", out)
        self.assertNotIn("aviso", out)

    def test_exemplo_de_configuracao(self):
        os.environ["SENHA_IHM_INJETORA2"] = "teste"
        self.addCleanup(os.environ.pop, "SENHA_IHM_INJETORA2", None)
        cfg = carregar(RAIZ / "exemplos" / "backup-clp.toml")
        self.assertEqual([m.nome for m in cfg.maquinas], ["Extrusora 1", "Extrusora 2", "Injetora 2", "Sopradora 3"])
        self.assertEqual(cfg.maquinas[0].pastas[0], "//srv-arquivos/clp/extrusora1", "UNC fica como está")
        self.assertEqual(cfg.maquinas[0].pastas[1], "C:/Projetos/Extrusora1")
        self.assertEqual(cfg.maquinas[1].padroes, ["*"])
        self.assertEqual(cfg.maquinas[2].ftp.senha, "teste")
        self.assertEqual(cfg.maquinas[2].ftp.host, "10.0.0.22")
        self.assertEqual(cfg.retencao.versoes, 200)
        self.assertEqual(rodar("-c", str(RAIZ / "exemplos" / "backup-clp.toml"), "verificar")[0], 0)


# ---------------------------------------------------------------- página web e exportação

class TestServidor(ComPasta):
    def test_api_e_download(self):
        criar_demo(self.tmp / "a")
        with Acervo(self.tmp / "a") as a:
            srv = criar_servidor(a, "127.0.0.1", 0, "Teste <&>")
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"

                def get(u):
                    with urllib.request.urlopen(base + u, timeout=5) as r:
                        return r.read(), r.headers

                pagina, _ = get("/")
                self.assertIn(b"Teste &lt;&amp;&gt;", pagina)
                maqs = json.loads(get("/api/maquinas")[0])["maquinas"]
                self.assertEqual([m["maquina"] for m in maqs], ["Extrusora 1", "Injetora 2", "Sopradora 3"])
                vs = json.loads(get("/api/versoes?maquina=Injetora%202")[0])
                self.assertEqual(len(vs), 3)
                self.assertGreater(vs[0]["id"], vs[-1]["id"], "mais nova primeiro")
                d = json.loads(get(f"/api/diff?a={vs[1]['id']}&b={vs[0]['id']}")[0])
                self.assertEqual(d["tipo"], "zip")
                corpo, cab = get(f"/baixar/{vs[0]['id']}")
                self.assertEqual(sha256(corpo), vs[0]["hash"])
                self.assertIn("injetora2_v", cab["Content-Disposition"])
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/baixar/9999")
                self.assertEqual(e.exception.code, 404)
                e.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/api/diff?a=x")
                self.assertEqual(e.exception.code, 400)
                e.exception.close()
            finally:
                srv.shutdown()
                srv.server_close()


@unittest.skipUnless(shutil.which("git"), "git não instalado (a exportação é opcional)")
class TestExportarGit(ComPasta):
    def test_exporta_incremental(self):
        rel = Relogio(datetime(2026, 2, 1, 10))
        with Acervo(self.tmp / "a", relogio=rel) as a:
            a.registrar("Extrusora 1", "prog/principal.xml", b"<p>1</p>\n", autor="joao")
            rel.t += timedelta(days=1)
            a.registrar("Extrusora 1", "prog/principal.xml", b"<p>2</p>\n", autor="ana")
            repo = self.tmp / "repo"
            self.assertEqual(exportar_git(a, repo), 2)
            self.assertEqual(exportar_git(a, repo), 0, "incremental")
            a.registrar("Injetora 2", "ihm.cmtp", b"\x00bin", autor="")
            self.assertEqual(exportar_git(a, repo), 1)
        import subprocess
        log = subprocess.run(["git", "log", "--format=%an|%ad|%s", "--date=short"], cwd=repo,
                             capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(log[-1], "joao|2026-02-01|Extrusora 1: prog/principal.xml (versão 1)")
        self.assertEqual(log[0].split("|")[0], "desconhecido")
        self.assertEqual((repo / "Extrusora 1" / "prog" / "principal.xml").read_bytes(), b"<p>2</p>\n")


class TestDemo(ComPasta):
    def test_demo_tem_os_tres_tipos_de_diff(self):
        criar_demo(self.tmp / "d")
        with Acervo(self.tmp / "d") as a:
            tipos = set()
            for v in a.versoes():
                ant = a.anterior(v)
                if ant:
                    tipos.add(comparar(a, ant.id, v.id)["tipo"])
            self.assertEqual(tipos, {"texto", "zip", "binario"})


if __name__ == "__main__":
    unittest.main()
