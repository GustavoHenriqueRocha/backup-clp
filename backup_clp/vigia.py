"""Vigilância das pastas por varredura periódica: mtime + tamanho para notar mudança, hash para decidir se é versão nova.

Um arquivo só é copiado quando está *estável*: mesmo mtime e tamanho por pelo menos `espera` segundos.
Isso evita guardar um projeto salvo pela metade (os softwares de CLP gravam arquivos grandes aos poucos,
e numa pasta de rede a gravação pode levar vários segundos).
"""
import fnmatch
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import pwd
except ImportError:      # Windows
    pwd = None


@dataclass
class Captura:
    maquina: str
    arquivo: str
    versao: object          # Versao gravada


def casa(nome, padroes):
    n = nome.casefold()
    return any(fnmatch.fnmatchcase(n, p.casefold()) for p in padroes)


def dono(st):
    """Usuário dono do arquivo, quando o sistema informa (Linux/macOS). No Windows fica vazio."""
    if pwd is None:
        return ""
    try:
        return pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        return str(st.st_uid)


def listar(pasta, padroes, ignorar, recursivo=True):
    """Arquivos da pasta que casam com os padrões, como (caminho, relativo com "/"). Funciona com caminho UNC."""
    raiz = Path(pasta)
    pilha = [raiz]
    while pilha:
        atual = pilha.pop()
        try:
            entradas = list(os.scandir(atual))
        except OSError:
            continue        # pasta de rede fora do ar ou sem permissão: tenta na próxima volta
        for e in entradas:
            if casa(e.name, ignorar):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if recursivo:
                        pilha.append(Path(e.path))
                elif e.is_file() and casa(e.name, padroes):
                    yield Path(e.path), Path(e.path).relative_to(raiz).as_posix()
            except OSError:
                continue


class Vigia:
    def __init__(self, cfg, acervo, relogio=time.time):
        self.cfg = cfg
        self.acervo = acervo
        self.relogio = relogio
        self.estacao = socket.gethostname()
        self._visto = {}        # caminho -> ((mtime_ns, tamanho), desde quando está assim, já vimos mudar)
        self._feito = {}        # caminho -> (mtime_ns, tamanho) já conferido contra o acervo
        self.erros = {}

    def varrer(self):
        """Uma volta por todas as pastas. Devolve a lista de Captura (versões novas)."""
        novas = []
        presentes = set()
        for m in self.cfg.maquinas:
            for pasta in m.pastas:
                varios = len(m.pastas) > 1
                for caminho, rel in listar(pasta, m.padroes, m.ignorar, m.recursivo):
                    presentes.add(caminho)
                    arquivo = f"{Path(pasta).name}/{rel}" if varios else rel
                    c = self._conferir(m, pasta, caminho, arquivo)
                    if c:
                        novas.append(c)
        for p in list(self._visto):
            if p not in presentes:
                del self._visto[p]
                self._feito.pop(p, None)
        return novas

    def _conferir(self, m, pasta, caminho, arquivo):
        try:
            st = caminho.stat()
        except OSError:
            return None
        assinatura = (st.st_mtime_ns, st.st_size)
        agora = self.relogio()
        anterior = self._visto.get(caminho)
        if anterior is None:
            # Primeira vez que vemos: confia no mtime (arquivo antigo já está pronto).
            self._visto[caminho] = (assinatura, agora, False)
            parado = agora - st.st_mtime
        elif anterior[0] != assinatura:
            # Vimos mudar: daqui em diante só vale o nosso relógio (cópias que preservam o mtime
            # e relógios diferentes entre servidor de arquivos e esta máquina não enganam).
            self._visto[caminho] = (assinatura, agora, True)
            parado = 0
        else:
            parado = agora - anterior[1] if anterior[2] else max(agora - anterior[1], agora - st.st_mtime)
        if self._feito.get(caminho) == assinatura or parado < self.cfg.espera_estavel:
            return None
        try:
            dados = caminho.read_bytes()
            depois = caminho.stat()
        except OSError as e:
            self.erros[str(caminho)] = str(e)
            return None
        if (depois.st_mtime_ns, depois.st_size) != assinatura or len(dados) != st.st_size:
            self._visto[caminho] = ((depois.st_mtime_ns, depois.st_size), agora, True)
            return None     # mudou enquanto lia: espera estabilizar de novo
        self.erros.pop(str(caminho), None)
        self._feito[caminho] = assinatura
        autor = dono(st) or m.autor
        v = self.acervo.registrar(m.nome, arquivo, dados, modificado=st.st_mtime, autor=autor,
                                  origem=f"{self.estacao}:{caminho}")
        return Captura(m.nome, arquivo, v) if v else None
