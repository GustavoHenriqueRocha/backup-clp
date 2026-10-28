"""Leitura e validação do arquivo TOML de configuração."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Extensões comuns de projeto de CLP e IHM. É só um ponto de partida: cada máquina pode ter as suas.
PADROES_COMUNS = [
    "*.wps",                        # WEG
    "*.emtp", "*.cmtp", "*.ecmp",   # Weintek EasyBuilder
    "*.isp", "*.dvp",               # Delta ISPSoft / WPLSoft
    "*.dpa", "*.dop",               # Delta DOPSoft
    "*.ap1*", "*.zap1*", "*.s7p",   # Siemens TIA Portal / STEP 7
    "*.V6F",
    "*.xml", "*.csv", "*.txt",      # exportações em texto
]
IGNORAR_COMUNS = ["~*", "*.tmp", "*.bak", "*.lck", "*.lock", ".~*", "Thumbs.db", "desktop.ini"]


class ErroConfig(ValueError):
    pass


@dataclass
class ConfigFTP:
    host: str
    porta: int = 21
    usuario: str = "anonymous"
    senha: str = ""
    caminhos: list = field(default_factory=list)   # arquivo, ou pasta terminada em "/"
    intervalo: float = 900.0
    timeout: float = 10.0
    passivo: bool = True


@dataclass
class ConfigHTTP:
    urls: list = field(default_factory=list)
    intervalo: float = 900.0
    timeout: float = 10.0


@dataclass
class ConfigMaquina:
    nome: str
    pastas: list = field(default_factory=list)
    padroes: list = field(default_factory=lambda: list(PADROES_COMUNS))
    ignorar: list = field(default_factory=lambda: list(IGNORAR_COMUNS))
    recursivo: bool = True
    autor: str = ""                 # quando o sistema não diz o dono do arquivo (Windows, FTP)
    ftp: ConfigFTP | None = None
    http: ConfigHTTP | None = None


@dataclass
class Retencao:
    versoes: int = 0                # por arquivo; 0 = sem limite
    dias: float = 0                 # 0 = sem limite; a versão mais nova de cada arquivo nunca sai


@dataclass
class Config:
    titulo: str = "Backup de programas"
    acervo: str = "acervo"
    intervalo: float = 60.0
    espera_estavel: float = 10.0
    ouvir: str = "127.0.0.1"
    porta: int = 8899
    retencao: Retencao = field(default_factory=Retencao)
    maquinas: list = field(default_factory=list)

    def maquina(self, nome):
        for m in self.maquinas:
            if m.nome.casefold() == nome.casefold():
                return m
        raise ErroConfig(f"máquina {nome!r} não está na configuração")


def _lista(valor, onde):
    if valor is None:
        return []
    if isinstance(valor, str):
        return [valor]
    if not isinstance(valor, list) or not all(isinstance(v, str) for v in valor):
        raise ErroConfig(f"{onde}: use um texto ou uma lista de textos")
    return list(valor)


def _numero(dados, chave, padrao, onde, minimo=0):
    valor = dados.get(chave, padrao)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < minimo:
        raise ErroConfig(f"{onde}: {chave} deve ser um número >= {minimo}")
    return valor


def _senha(dados, onde):
    if "senha_env" in dados:
        nome = dados["senha_env"]
        if nome not in os.environ:
            raise ErroConfig(f"{onde}: variável de ambiente {nome} não definida")
        return os.environ[nome]
    return str(dados.get("senha", ""))


def _ftp(dados, onde):
    if dados is None:
        return None
    if "host" not in dados:
        raise ErroConfig(f"{onde}: falta host")
    caminhos = _lista(dados.get("caminhos"), f"{onde}.caminhos")
    if not caminhos:
        raise ErroConfig(f"{onde}: informe ao menos um caminho em caminhos")
    return ConfigFTP(host=str(dados["host"]), porta=int(_numero(dados, "porta", 21, onde, 1)),
                     usuario=str(dados.get("usuario", "anonymous")), senha=_senha(dados, onde),
                     caminhos=caminhos, intervalo=_numero(dados, "intervalo", 900, onde),
                     timeout=_numero(dados, "timeout", 10, onde, 0.1), passivo=bool(dados.get("passivo", True)))


def _http(dados, onde):
    if dados is None:
        return None
    urls = _lista(dados.get("urls"), f"{onde}.urls")
    if not urls:
        raise ErroConfig(f"{onde}: informe ao menos uma URL em urls")
    return ConfigHTTP(urls=urls, intervalo=_numero(dados, "intervalo", 900, onde),
                      timeout=_numero(dados, "timeout", 10, onde, 0.1))


def interpretar(dados, pasta=Path(".")):
    """Transforma o dicionário do TOML em Config, com mensagens de erro claras."""
    cfg = Config(titulo=str(dados.get("titulo", "Backup de programas")),
                 intervalo=_numero(dados, "intervalo", 60, "intervalo", 0.1),
                 espera_estavel=_numero(dados, "espera_estavel", 10, "espera_estavel"))
    cfg.acervo = str((pasta / str(dados.get("acervo", "acervo"))).resolve())
    painel = dados.get("painel", {})
    cfg.ouvir = str(painel.get("ouvir", cfg.ouvir))
    cfg.porta = int(painel.get("porta", cfg.porta))
    ret = dados.get("retencao", {})
    cfg.retencao = Retencao(int(_numero(ret, "versoes", 0, "retencao")), _numero(ret, "dias", 0, "retencao"))

    nomes = set()
    for i, m in enumerate(dados.get("maquina", [])):
        onde = f"maquina[{i}]"
        nome = str(m.get("nome", "")).strip()
        if not nome:
            raise ErroConfig(f"{onde}: falta nome")
        if nome.casefold() in nomes:
            raise ErroConfig(f"{onde}: máquina {nome!r} repetida")
        nomes.add(nome.casefold())
        onde = f"máquina {nome!r}"
        pastas = [p if _remota(p) else str((pasta / p).resolve()) for p in _lista(m.get("pastas"), onde)]
        maq = ConfigMaquina(nome=nome, pastas=pastas, recursivo=bool(m.get("recursivo", True)),
                            autor=str(m.get("autor", "")),
                            ftp=_ftp(m.get("ftp"), f"{onde}.ftp"), http=_http(m.get("http"), f"{onde}.http"))
        if "padroes" in m:
            maq.padroes = _lista(m["padroes"], f"{onde}.padroes")
        if "ignorar" in m:
            maq.ignorar = _lista(m["ignorar"], f"{onde}.ignorar")
        if not (maq.pastas or maq.ftp or maq.http):
            raise ErroConfig(f"{onde}: informe pastas, [ftp] ou [http]")
        cfg.maquinas.append(maq)
    if not cfg.maquinas:
        raise ErroConfig("nenhuma [[maquina]] configurada")
    return cfg


def _remota(caminho):
    """Caminho absoluto ou de rede (UNC) fica como está; relativo é a partir do TOML."""
    return caminho.startswith(("\\\\", "//")) or Path(caminho).is_absolute() or (len(caminho) > 1 and caminho[1] == ":")


def carregar(caminho):
    caminho = Path(caminho)
    try:
        with open(caminho, "rb") as f:
            dados = tomllib.load(f)
    except FileNotFoundError:
        raise ErroConfig(f"configuração {caminho} não encontrada") from None
    except tomllib.TOMLDecodeError as e:
        raise ErroConfig(f"{caminho}: TOML inválido: {e}") from None
    return interpretar(dados, caminho.resolve().parent)
