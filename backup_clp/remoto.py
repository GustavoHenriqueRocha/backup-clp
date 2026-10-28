"""Coleta opcional direto da IHM/CLP: FTP (ftplib) e HTTP (urllib).

Muitas IHMs expõem o projeto ou o backup por FTP. Aqui o arquivo é baixado inteiro e comparado pelo hash:
os projetos são pequenos e nem toda IHM responde a SIZE/MDTM de forma confiável.
"""
import ftplib
import io
import posixpath
import time
import urllib.parse
import urllib.request

from .vigia import Captura, casa


class ErroColeta(Exception):
    pass


def baixar_ftp(cfg_ftp, padroes=("*",)):
    """Baixa os caminhos configurados. Caminho terminado em "/" é pasta: baixa o que casar com `padroes`.
    Devolve lista de (caminho remoto, bytes)."""
    saida = []
    with ftplib.FTP(timeout=cfg_ftp.timeout) as ftp:
        ftp.connect(cfg_ftp.host, cfg_ftp.porta)
        ftp.login(cfg_ftp.usuario, cfg_ftp.senha)
        ftp.set_pasv(cfg_ftp.passivo)
        for caminho in cfg_ftp.caminhos:
            if caminho.endswith("/"):
                nomes = [posixpath.basename(n.rstrip("/")) for n in ftp.nlst(caminho)]
                alvos = [posixpath.join(caminho, n) for n in sorted(set(nomes)) if n and casa(n, padroes)]
            else:
                alvos = [caminho]
            for alvo in alvos:
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {alvo}", buf.write)
                saida.append((alvo, buf.getvalue()))
    return saida


def baixar_http(cfg_http):
    saida = []
    for url in cfg_http.urls:
        with urllib.request.urlopen(url, timeout=cfg_http.timeout) as r:
            saida.append((url, r.read()))
    return saida


class Coletor:
    """Faz a coleta remota de cada máquina no intervalo dela e registra no acervo."""

    def __init__(self, cfg, acervo, relogio=time.monotonic):
        self.cfg = cfg
        self.acervo = acervo
        self.relogio = relogio
        self._ultima = {}
        self.erros = {}

    def coletar(self, forcar=False):
        novas = []
        agora = self.relogio()
        for m in self.cfg.maquinas:
            for tipo, c in (("ftp", m.ftp), ("http", m.http)):
                if c is None:
                    continue
                chave = (m.nome, tipo)
                if not forcar and chave in self._ultima and agora - self._ultima[chave] < c.intervalo:
                    continue
                self._ultima[chave] = agora
                try:
                    novas += self._uma(m, tipo, c)
                    self.erros.pop(chave, None)
                except (OSError, EOFError, ftplib.Error) as e:   # IHM desligada não derruba o resto
                    self.erros[chave] = f"{type(e).__name__}: {e}"
        return novas

    def _uma(self, m, tipo, c):
        novas = []
        if tipo == "ftp":
            itens = baixar_ftp(c, m.padroes)
            base = f"ftp://{c.host}" + (f":{c.porta}" if c.porta != 21 else "")
        else:
            itens = baixar_http(c)
            base = ""
        for caminho, dados in itens:
            if tipo == "ftp":
                arquivo, origem = f"ftp/{c.host}/{caminho.lstrip('/')}", base + "/" + caminho.lstrip("/")
            else:
                u = urllib.parse.urlsplit(caminho)
                arquivo, origem = f"http/{u.hostname}{u.path or '/'}".rstrip("/"), caminho
            v = self.acervo.registrar(m.nome, arquivo, dados, autor=m.autor, origem=origem)
            if v:
                novas.append(Captura(m.nome, arquivo, v))
        return novas
