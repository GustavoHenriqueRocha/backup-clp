"""Página web: linha do tempo por máquina, baixar qualquer versão e comparar duas."""
import html
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from .acervo import ErroAcervo
from .diferenca import comparar

PAGINA = (Path(__file__).parent / "pagina.html").read_text(encoding="utf-8")


def nome_download(v):
    p = PurePosixPath(v.arquivo)
    return f"{p.stem}_v{v.id}{p.suffix}"


def criar_servidor(acervo, host="127.0.0.1", porta=8899, titulo="Backup de programas", estado=None):
    """`estado` é uma função opcional que devolve erros/últimas capturas do vigia, para mostrar na página."""
    pagina = PAGINA.replace("{{titulo}}", html.escape(titulo)).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _json(self, obj, codigo=200):
            corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._enviar(codigo, "application/json; charset=utf-8", corpo)

        def _enviar(self, codigo, tipo, corpo, extra=()):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            try:
                if u.path == "/":
                    self._enviar(200, "text/html; charset=utf-8", pagina)
                elif u.path == "/api/maquinas":
                    self._json({"titulo": titulo, "maquinas": acervo.maquinas(),
                                "estado": estado() if estado else {}})
                elif u.path == "/api/versoes":
                    vs = [v.dict() for v in acervo.versoes(q.get("maquina"))]
                    self._json(list(reversed(vs)))
                elif u.path == "/api/diff":
                    self._json(comparar(acervo, int(q["a"]), int(q["b"])))
                elif u.path.startswith("/baixar/"):
                    v = acervo.versao(int(u.path.rsplit("/", 1)[1]))
                    nome = nome_download(v)
                    cd = f"attachment; filename=\"{nome.encode('ascii', 'replace').decode()}\"; " \
                         f"filename*=UTF-8''{urllib.parse.quote(nome)}"
                    self._enviar(200, "application/octet-stream", acervo.ler(v.hash),
                                 [("Content-Disposition", cd), ("X-SHA256", v.hash)])
                else:
                    self._json({"erro": "não encontrado"}, 404)
            except (KeyError, ValueError):
                self._json({"erro": "parâmetros inválidos"}, 400)
            except ErroAcervo as e:
                self._json({"erro": str(e)}, 404)

    return ThreadingHTTPServer((host, porta), Handler)
