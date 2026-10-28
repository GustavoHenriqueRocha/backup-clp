"""Acervo: conteúdo guardado pelo hash SHA-256 (cada conteúdo uma vez só) e índice das versões em SQLite."""
import hashlib
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timedelta
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS versao (
    id          INTEGER PRIMARY KEY,
    maquina     TEXT NOT NULL,
    arquivo     TEXT NOT NULL,      -- caminho relativo, sempre com "/"
    hash        TEXT NOT NULL,
    tamanho     INTEGER NOT NULL,
    modificado  TEXT,               -- mtime do arquivo na origem (ISO 8601), quando se sabe
    capturado   TEXT NOT NULL,      -- quando entrou no acervo
    autor       TEXT NOT NULL DEFAULT '',
    origem      TEXT NOT NULL DEFAULT ''   -- pasta, ftp://… ou http://… de onde veio
);
CREATE INDEX IF NOT EXISTS versao_arquivo ON versao (maquina, arquivo, id);
CREATE INDEX IF NOT EXISTS versao_hash ON versao (hash);
"""


class ErroAcervo(Exception):
    pass


@dataclass
class Versao:
    id: int
    maquina: str
    arquivo: str
    hash: str
    tamanho: int
    modificado: str | None
    capturado: str
    autor: str
    origem: str

    def dict(self):
        return dict(self.__dict__)


def _travado(f):
    """O acervo é usado ao mesmo tempo pelo vigia e pelo servidor web: uma operação por vez no SQLite."""
    @wraps(f)
    def g(self, *a, **k):
        with self._lock:
            return f(self, *a, **k)
    return g


def sha256(dados):
    return hashlib.sha256(dados).hexdigest()


class Acervo:
    def __init__(self, pasta, relogio=datetime.now):
        self.pasta = Path(pasta)
        self.objetos = self.pasta / "objetos"
        self.objetos.mkdir(parents=True, exist_ok=True)
        self.relogio = relogio
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.pasta / "indice.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(ESQUEMA)

    @_travado
    def fechar(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()

    # --- conteúdo ---

    def _objeto(self, h):
        return self.objetos / h[:2] / h[2:]

    def guardar_conteudo(self, dados):
        """Grava o conteúdo pelo hash, se ainda não existir. Escrita atômica: nunca fica objeto pela metade."""
        h = sha256(dados)
        destino = self._objeto(h)
        if not destino.exists():
            destino.parent.mkdir(exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=".novo-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(dados)
                os.replace(tmp, destino)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return h

    def ler(self, h):
        try:
            dados = self._objeto(h).read_bytes()
        except FileNotFoundError:
            raise ErroAcervo(f"conteúdo {h[:12]} não está no acervo") from None
        if sha256(dados) != h:
            raise ErroAcervo(f"conteúdo {h[:12]} corrompido no disco (hash não confere)")
        return dados

    # --- versões ---

    @_travado
    def ultima(self, maquina, arquivo):
        r = self.db.execute("SELECT * FROM versao WHERE maquina=? AND arquivo=? ORDER BY id DESC LIMIT 1",
                            (maquina, arquivo)).fetchone()
        return Versao(**r) if r else None

    @_travado
    def registrar(self, maquina, arquivo, dados, modificado=None, autor="", origem=""):
        """Guarda uma versão nova. Devolve a Versao, ou None se o conteúdo é igual ao da última."""
        h = sha256(dados)
        ultima = self.ultima(maquina, arquivo)
        if ultima and ultima.hash == h:
            return None
        self.guardar_conteudo(dados)
        if isinstance(modificado, (int, float)):
            modificado = datetime.fromtimestamp(modificado).isoformat(timespec="seconds")
        capturado = self.relogio().isoformat(timespec="seconds")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO versao (maquina, arquivo, hash, tamanho, modificado, capturado, autor, origem) "
                "VALUES (?,?,?,?,?,?,?,?)", (maquina, arquivo, h, len(dados), modificado, capturado, autor, origem))
        return self.versao(cur.lastrowid)

    @_travado
    def versao(self, id_):
        r = self.db.execute("SELECT * FROM versao WHERE id=?", (int(id_),)).fetchone()
        if not r:
            raise ErroAcervo(f"versão {id_} não existe")
        return Versao(**r)

    @_travado
    def versoes(self, maquina=None, arquivo=None):
        sql, args = "SELECT * FROM versao WHERE 1=1", []
        if maquina is not None:
            sql += " AND maquina=? COLLATE NOCASE"
            args.append(maquina)
        if arquivo is not None:
            sql += " AND arquivo=?"
            args.append(arquivo)
        return [Versao(**r) for r in self.db.execute(sql + " ORDER BY id", args)]

    @_travado
    def maquinas(self):
        return [dict(r) for r in self.db.execute(
            "SELECT maquina, COUNT(*) AS versoes, COUNT(DISTINCT arquivo) AS arquivos, MAX(capturado) AS ultima "
            "FROM versao GROUP BY maquina ORDER BY maquina")]

    @_travado
    def anterior(self, v):
        r = self.db.execute("SELECT * FROM versao WHERE maquina=? AND arquivo=? AND id<? ORDER BY id DESC LIMIT 1",
                            (v.maquina, v.arquivo, v.id)).fetchone()
        return Versao(**r) if r else None

    def tamanho_em_disco(self):
        return sum(p.stat().st_size for p in self.objetos.glob("*/*"))

    # --- retenção ---

    @_travado
    def aplicar_retencao(self, versoes=0, dias=0):
        """Apaga versões além das `versoes` mais novas de cada arquivo e as mais velhas que `dias`.
        A versão mais nova de cada arquivo nunca é apagada. Devolve (versões apagadas, objetos apagados)."""
        limite = (self.relogio() - timedelta(days=dias)).isoformat(timespec="seconds") if dias else None
        apagar = []
        grupos = self.db.execute("SELECT DISTINCT maquina, arquivo FROM versao").fetchall()
        for g in grupos:
            lista = self.db.execute("SELECT id, capturado FROM versao WHERE maquina=? AND arquivo=? ORDER BY id DESC",
                                    (g["maquina"], g["arquivo"])).fetchall()
            for pos, r in enumerate(lista):
                if pos == 0:
                    continue
                if (versoes and pos >= versoes) or (limite and r["capturado"] < limite):
                    apagar.append(r["id"])
        with self.db:
            self.db.executemany("DELETE FROM versao WHERE id=?", [(i,) for i in apagar])
        usados = {r[0] for r in self.db.execute("SELECT DISTINCT hash FROM versao")}
        objetos = 0
        for p in self.objetos.glob("*/*"):
            if p.name.startswith(".novo-"):
                continue
            if p.parent.name + p.name not in usados:
                p.unlink()
                objetos += 1
        return len(apagar), objetos
