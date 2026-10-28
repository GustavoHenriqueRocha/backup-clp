"""Exportação opcional para um repositório git (só se o `git` estiver instalado).

Cada versão vira um commit, na ordem em que entrou no acervo, com a data de captura e o autor.
É incremental: guarda no próprio repositório o último id exportado.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

MARCA = ".backup-clp-exportado"


class ErroExportacao(Exception):
    pass


def _seguro(parte):
    parte = re.sub(r'[<>:"|?*\x00-\x1f]', "_", parte).strip(" .")
    return parte or "_"


def caminho_no_repo(v):
    partes = [_seguro(v.maquina)] + [_seguro(p) for p in v.arquivo.split("/") if p not in ("", ".", "..")]
    return Path(*partes)


def exportar_git(acervo, destino, git=None):
    git = git or shutil.which("git")
    if not git:
        raise ErroExportacao("git não encontrado no PATH: a exportação é opcional, o acervo funciona sem ele")
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)

    def rodar(*args, env=None):
        r = subprocess.run([git, *args], cwd=destino, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise ErroExportacao(f"git {args[0]}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    if not (destino / ".git").exists():
        rodar("init", "-q")
    marca = destino / MARCA
    ultimo = int(marca.read_text().strip() or 0) if marca.exists() else 0
    feitas = 0
    for v in acervo.versoes():
        if v.id <= ultimo:
            continue
        alvo = destino / caminho_no_repo(v)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(acervo.ler(v.hash))
        marca.write_text(f"{v.id}\n")
        autor = v.autor or "desconhecido"
        env = {**os.environ, "GIT_AUTHOR_NAME": autor, "GIT_AUTHOR_EMAIL": f"{_seguro(autor)}@backup-clp",
               "GIT_AUTHOR_DATE": v.capturado, "GIT_COMMITTER_NAME": "backup-clp",
               "GIT_COMMITTER_EMAIL": "backup-clp@localhost", "GIT_COMMITTER_DATE": v.capturado}
        rodar("add", "--", str(caminho_no_repo(v)), MARCA)
        rodar("-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m",
              f"{v.maquina}: {v.arquivo} (versão {v.id})\n\nsha256 {v.hash}\norigem {v.origem}", env=env)
        feitas += 1
    return feitas
