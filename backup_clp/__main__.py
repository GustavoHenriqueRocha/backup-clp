"""Linha de comando: vigiar, painel, versoes, diff, restaurar, retencao, exportar-git, verificar e demo."""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .acervo import Acervo, ErroAcervo
from .config import ErroConfig, carregar
from .diferenca import comparar, em_texto
from .exportar import ErroExportacao, exportar_git
from .remoto import Coletor
from .servidor import criar_servidor, nome_download
from .vigia import Vigia

CONFIG_PADRAO = os.environ.get("BACKUP_CLP_CONFIG", "backup-clp.toml")


def _log(msg):
    print(f"{datetime.now():%d/%m/%Y %H:%M:%S} {msg}", flush=True)


def _tam(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MiB"


def _acervo(a):
    if a.acervo:
        return Acervo(a.acervo)
    return Acervo(carregar(a.config).acervo)


def _servir_em_thread(acervo, host, porta, titulo, estado=None):
    srv = criar_servidor(acervo, host, porta, titulo, estado)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _log(f"página em http://{host}:{srv.server_address[1]}")
    return srv


def cmd_vigiar(a):
    cfg = carregar(a.config)
    with Acervo(cfg.acervo) as acervo:
        vigia, coletor = Vigia(cfg, acervo), Coletor(cfg, acervo)

        def estado():
            erros = {}
            for (maq, tipo), e in coletor.erros.items():
                erros[maq] = f"{tipo}: {e}"
            return {"erros": erros, "pastas_com_erro": dict(vigia.erros)}

        srv = None if (a.uma_vez or a.sem_painel) else _servir_em_thread(
            acervo, a.ouvir or cfg.ouvir, a.porta or cfg.porta, cfg.titulo, estado)
        _log(f"vigiando {len(cfg.maquinas)} máquina(s) a cada {cfg.intervalo:g} s "
             f"(arquivo estável após {cfg.espera_estavel:g} s); acervo {cfg.acervo}")
        erros_vistos = {}
        try:
            while True:
                novas = vigia.varrer() + coletor.coletar()
                for c in novas:
                    v = c.versao
                    _log(f"{c.maquina}: nova versão #{v.id} de {c.arquivo} ({_tam(v.tamanho)}, {v.hash[:12]}, "
                         f"{v.autor or 'autor ?'})")
                for chave, e in {**coletor.erros, **vigia.erros}.items():
                    if erros_vistos.get(chave) != e:
                        _log(f"falha em {chave}: {e}")
                erros_vistos = {**coletor.erros, **vigia.erros}
                if novas and (cfg.retencao.versoes or cfg.retencao.dias):
                    v, o = acervo.aplicar_retencao(cfg.retencao.versoes, cfg.retencao.dias)
                    if v:
                        _log(f"retenção: {v} versão(ões) e {o} conteúdo(s) apagados")
                if a.uma_vez:
                    print(f"{len(novas)} versão(ões) nova(s)")
                    return 0
                time.sleep(cfg.intervalo)
        except KeyboardInterrupt:
            return 0
        finally:
            if srv:
                srv.shutdown()
                srv.server_close()


def cmd_painel(a):
    cfg = carregar(a.config)
    with Acervo(cfg.acervo) as acervo:
        srv = criar_servidor(acervo, a.ouvir or cfg.ouvir, a.porta or cfg.porta, cfg.titulo)
        _log(f"página em http://{srv.server_address[0]}:{srv.server_address[1]}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    return 0


def cmd_demo(a):
    from .demo import criar_demo
    pasta = Path(a.pasta) if a.pasta else Path(tempfile.mkdtemp(prefix="backup-clp-demo-"))
    if not (pasta / "indice.db").exists():
        criar_demo(pasta)
    print(f"acervo de demonstração em {pasta}")
    with Acervo(pasta) as acervo:
        srv = criar_servidor(acervo, a.ouvir or "127.0.0.1", a.porta or 8899, "Backup de programas — demonstração")
        _log(f"página em http://{srv.server_address[0]}:{srv.server_address[1]}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    return 0


def cmd_versoes(a):
    with _acervo(a) as acervo:
        vs = acervo.versoes(a.maquina, a.arquivo)
        if not vs:
            print(f"nenhuma versão de {a.maquina}" + (f" / {a.arquivo}" if a.arquivo else ""))
            return 1
        if a.json:
            print(json.dumps([v.dict() for v in vs], ensure_ascii=False, indent=2))
            return 0
        print(f"{'id':>5}  {'capturado':<19}  {'tamanho':>10}  {'sha256':<12}  {'autor':<14}  arquivo")
        for v in vs:
            print(f"{v.id:>5}  {v.capturado.replace('T', ' '):<19}  {_tam(v.tamanho):>10}  {v.hash[:12]}  "
                  f"{(v.autor or '?')[:14]:<14}  {v.arquivo}")
    return 0


def cmd_diff(a):
    with _acervo(a) as acervo:
        if a.b is None:
            vb = acervo.versao(a.a)
            va = acervo.anterior(vb)
            if not va:
                print(f"versão {vb.id} é a primeira de {vb.arquivo}: nada para comparar", file=sys.stderr)
                return 1
            ida, idb = va.id, vb.id
        else:
            ida, idb = a.a, a.b
        r = comparar(acervo, ida, idb)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"#{r['a']['id']} {r['a']['arquivo']} ({r['a']['capturado']})  ->  "
              f"#{r['b']['id']} {r['b']['arquivo']} ({r['b']['capturado']})")
        print("\n".join(em_texto(r)))
    return 0


def cmd_restaurar(a):
    with _acervo(a) as acervo:
        v = acervo.versao(a.id)
        dados = acervo.ler(v.hash)        # confere o hash antes de escrever
    destino = Path(a.destino) if a.destino else Path(nome_download(v))
    if destino.is_dir():
        destino = destino / Path(v.arquivo).name
    if destino.exists() and not a.forcar:
        print(f"{destino} já existe: use --forcar para sobrescrever", file=sys.stderr)
        return 1
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".restaurando")
    tmp.write_bytes(dados)
    os.replace(tmp, destino)
    print(f"versão #{v.id} de {v.maquina} / {v.arquivo} ({v.capturado}) restaurada em {destino} "
          f"({_tam(len(dados))}, sha256 {v.hash[:12]})")
    return 0


def cmd_retencao(a):
    cfg = carregar(a.config)
    versoes = cfg.retencao.versoes if a.versoes is None else a.versoes
    dias = cfg.retencao.dias if a.dias is None else a.dias
    with Acervo(cfg.acervo) as acervo:
        v, o = acervo.aplicar_retencao(versoes, dias)
    print(f"{v} versão(ões) e {o} conteúdo(s) apagados (mantendo {versoes or 'todas'} versões, "
          f"{f'{dias:g} dias' if dias else 'sem limite de idade'})")
    return 0


def cmd_exportar(a):
    with _acervo(a) as acervo:
        n = exportar_git(acervo, a.destino)
    print(f"{n} versão(ões) exportada(s) para {a.destino}")
    return 0


def cmd_verificar(a):
    cfg = carregar(a.config)
    print(f"{cfg.titulo}: {len(cfg.maquinas)} máquina(s), varredura a cada {cfg.intervalo:g} s, "
          f"estável após {cfg.espera_estavel:g} s, acervo {cfg.acervo}")
    r = cfg.retencao
    if r.versoes or r.dias:
        print(f"  retenção: {r.versoes or 'todas as'} versões por arquivo" + (f", até {r.dias:g} dias" if r.dias else ""))
    for m in cfg.maquinas:
        print(f"  {m.nome}")
        for p in m.pastas:
            print(f"    pasta {p}{'' if Path(p).exists() else '  (inacessível agora)'}")
        print(f"    padrões {' '.join(m.padroes)}")
        if m.ftp:
            print(f"    ftp {m.ftp.usuario}@{m.ftp.host}:{m.ftp.porta} {' '.join(m.ftp.caminhos)} "
                  f"a cada {m.ftp.intervalo:g} s")
        if m.http:
            print(f"    http {' '.join(m.http.urls)} a cada {m.http.intervalo:g} s")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="backup-clp",
                                description="Versionamento automático dos programas de CLP e IHM.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default=CONFIG_PADRAO, help=f"arquivo TOML (padrão: {CONFIG_PADRAO})")
    sub = p.add_subparsers(dest="comando", required=True)

    def acervo_opc(sp):
        sp.add_argument("--acervo", help="pasta do acervo (em vez de ler da configuração)")
        sp.add_argument("--json", action="store_true", help="saída em JSON")

    def web(sp):
        sp.add_argument("--ouvir", help="endereço da página (0.0.0.0 para a rede)")
        sp.add_argument("--porta", type=int)

    sp = sub.add_parser("vigiar", help="vigia as pastas e IHMs e guarda cada versão nova (com a página web)")
    sp.add_argument("--uma-vez", action="store_true", help="uma varredura só e sai")
    sp.add_argument("--sem-painel", action="store_true", help="não sobe a página web")
    web(sp)
    sp.set_defaults(f=cmd_vigiar)

    sp = sub.add_parser("painel", help="só a página web, sem vigiar")
    web(sp)
    sp.set_defaults(f=cmd_painel)

    sp = sub.add_parser("versoes", help="lista as versões de uma máquina")
    sp.add_argument("maquina")
    sp.add_argument("arquivo", nargs="?", help="só deste arquivo")
    acervo_opc(sp)
    sp.set_defaults(f=cmd_versoes)

    sp = sub.add_parser("diff", help="o que mudou entre as versões A e B (só A: compara com a anterior)")
    sp.add_argument("a", type=int)
    sp.add_argument("b", type=int, nargs="?")
    acervo_opc(sp)
    sp.set_defaults(f=cmd_diff)

    sp = sub.add_parser("restaurar", help="grava a versão ID num arquivo")
    sp.add_argument("id", type=int)
    sp.add_argument("destino", nargs="?", help="arquivo ou pasta de destino (padrão: nome_vID.ext aqui)")
    sp.add_argument("--forcar", action="store_true", help="sobrescreve se já existir")
    sp.add_argument("--acervo")
    sp.set_defaults(f=cmd_restaurar)

    sp = sub.add_parser("retencao", help="aplica a retenção agora")
    sp.add_argument("--versoes", type=int)
    sp.add_argument("--dias", type=float)
    sp.set_defaults(f=cmd_retencao)

    sp = sub.add_parser("exportar-git", help="exporta as versões como commits num repositório git (precisa do git)")
    sp.add_argument("destino")
    sp.add_argument("--acervo")
    sp.set_defaults(f=cmd_exportar)

    sp = sub.add_parser("verificar", help="mostra o que foi entendido da configuração")
    sp.set_defaults(f=cmd_verificar)

    sp = sub.add_parser("demo", help="página com um acervo de demonstração (sem máquina nenhuma)")
    sp.add_argument("--pasta", help="onde criar o acervo de demonstração (padrão: pasta temporária)")
    web(sp)
    sp.set_defaults(f=cmd_demo)

    a = p.parse_args(argv)
    try:
        return a.f(a)
    except (ErroConfig, ErroAcervo, ErroExportacao) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
