"""Acervo de demonstração: três máquinas genéricas com histórico de texto, ZIP e binário. Nada aqui é projeto real."""
import io
import random
import zipfile
from datetime import datetime, timedelta

from .acervo import Acervo


def _receitas(temp, vel):
    linhas = ["receita;zona1;zona2;zona3;zona4;velocidade_rpm"]
    for i, nome in enumerate(["PEAD-200", "PEAD-350", "PP-120", "PP-180"]):
        z = [temp + 5 * k + i for k in range(4)]
        linhas.append(f"{nome};" + ";".join(map(str, z)) + f";{vel + 3 * i}")
    return "\r\n".join(linhas) + "\r\n"


def _ladder(rede_extra=False, atraso=500):
    xml = ['<?xml version="1.0" encoding="utf-8"?>', "<programa nome=\"PRINCIPAL\">",
           '  <rede n="1"><contato tag="BT_LIGA"/><contato tag="EMERG_OK"/><bobina tag="MOTOR_PRINC"/></rede>',
           f'  <rede n="2"><contato tag="MOTOR_PRINC"/><temporizador tag="T_PARTIDA" pt="{atraso}"/></rede>',
           '  <rede n="3"><contato tag="T_PARTIDA.Q"/><bobina tag="BOMBA_OLEO"/></rede>']
    if rede_extra:
        xml.append('  <rede n="4"><contato tag="SENSOR_PORTA" negado="1"/><bobina tag="ALARME_PORTA"/></rede>')
    xml.append("</programa>")
    return "\n".join(xml) + "\n"


LOGO = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))   # imagem de mentira


def _projeto_ihm(telas, alarmes, logo=LOGO):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("projeto.xml", f'<projeto modelo="IHM-7pol" telas="{len(telas)}"/>\n')
        for i, t in enumerate(telas, 1):
            z.writestr(f"telas/tela{i:02d}.xml", f'<tela nome="{t}">\n  <botao tag="BT_{t.upper()}"/>\n</tela>\n')
        z.writestr("alarmes.csv", "codigo;texto\n" + "".join(f"{i};{a}\n" for i, a in enumerate(alarmes, 1)))
        z.writestr("imagens/logo.png", logo)
    return buf.getvalue()


def _binario(semente, mudar=()):
    rnd = random.Random(semente)
    dados = bytearray(rnd.randbytes(24 * 1024))
    for pos, novo in mudar:
        dados[pos:pos + len(novo)] = novo
    return bytes(dados)


def criar_demo(pasta, agora=None):
    agora = agora or datetime.now().replace(microsecond=0)
    momento = [(agora - timedelta(days=40)).replace(hour=0, minute=0, second=0)]

    def relogio():
        return momento[0]

    passos = [
        (0, "Extrusora 1", "receitas.csv", _receitas(180, 60).encode(), "joao.silva", "ENG-01:\\\\srv-arquivos\\clp\\extrusora1\\receitas.csv"),
        (0.1, "Extrusora 1", "programa/principal.xml", _ladder().encode(), "joao.silva", "ENG-01:\\\\srv-arquivos\\clp\\extrusora1\\programa\\principal.xml"),
        (0.2, "Injetora 2", "ihm/injetora2.emtp", _projeto_ihm(["Inicio", "Molde", "Alarmes"], ["Porta aberta", "Temperatura alta"]), "", "ftp://10.0.0.22/project/injetora2.emtp"),
        (0.3, "Sopradora 3", "clp/sopradora3.V6F", _binario(7), "ana.costa", "ENG-02:C:\\Projetos\\Sopradora3\\sopradora3.V6F"),
        (6, "Extrusora 1", "receitas.csv", _receitas(185, 60).encode(), "joao.silva", "ENG-01:\\\\srv-arquivos\\clp\\extrusora1\\receitas.csv"),
        (11, "Injetora 2", "ihm/injetora2.emtp", _projeto_ihm(["Inicio", "Molde", "Alarmes", "Receitas"], ["Porta aberta", "Temperatura alta", "Pressão baixa"]), "", "ftp://10.0.0.22/project/injetora2.emtp"),
        (15, "Sopradora 3", "clp/sopradora3.V6F", _binario(7, [(4096, b"\x01\x02\x03\x04"), (9000, b"\xff" * 12)]), "ana.costa", "ENG-02:C:\\Projetos\\Sopradora3\\sopradora3.V6F"),
        (22, "Extrusora 1", "programa/principal.xml", _ladder(True, 800).encode(), "carlos.manut", "ENG-03:\\\\srv-arquivos\\clp\\extrusora1\\programa\\principal.xml"),
        (29, "Injetora 2", "ihm/injetora2.emtp", _projeto_ihm(["Inicio", "Molde", "Alarmes", "Receitas"], ["Porta aberta", "Temperatura alta", "Pressão baixa do óleo"], LOGO[:30] + b"\xee\xee" + LOGO[32:]), "", "ftp://10.0.0.22/project/injetora2.emtp"),
        (34, "Extrusora 1", "receitas.csv", _receitas(185, 64).encode(), "carlos.manut", "ENG-03:\\\\srv-arquivos\\clp\\extrusora1\\receitas.csv"),
        (39.5, "Sopradora 3", "clp/sopradora3.V6F", _binario(7, [(4096, b"\x01\x02\x03\x04"), (9000, b"\xff" * 12), (20000, b"\x10\x20")]), "ana.costa", "ENG-02:C:\\Projetos\\Sopradora3\\sopradora3.V6F"),
    ]
    with Acervo(pasta, relogio=relogio) as a:
        inicio = momento[0]
        for dias, maq, arq, dados, autor, origem in passos:
            momento[0] = inicio + timedelta(days=dias, hours=9 + int(dias * 7) % 8, minutes=int(dias * 37) % 60)
            a.registrar(maq, arq, dados, modificado=momento[0].timestamp(), autor=autor or "ihm", origem=origem)
    return pasta
