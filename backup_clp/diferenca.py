"""O que mudou entre duas versões.

- texto/XML/CSV: diff unificado;
- ZIP (vários formatos de projeto são ZIP por dentro): entradas adicionadas, removidas e alteradas,
  com diff das que forem texto;
- binário: tamanho, hash e as regiões (offsets) alteradas, resumidas.
"""
import difflib
import io
import re
import zipfile

from .acervo import sha256

MAX_LINHAS = 4000           # diff maior que isso é cortado (a página e o terminal agradecem)
MAX_REGIOES = 50
JUNTAR = 16                 # regiões binárias separadas por menos que isso viram uma só
BLOCO = 4096


def decodificar(dados):
    """Texto legível ou None. Aceita UTF-8, UTF-16 com BOM (comum em exportação do Windows) e Latin-1."""
    if dados.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return dados.decode("utf-16")
        except UnicodeDecodeError:
            return None
    amostra = dados[:8192]
    if b"\x00" in amostra:
        return None
    try:
        return dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    texto = dados.decode("latin-1")
    controle = sum(1 for ch in texto[:8192] if ord(ch) < 32 and ch not in "\r\n\t\f")
    return texto if controle <= len(texto[:8192]) * 0.02 else None


def eh_zip(dados):
    return dados[:4] == b"PK\x03\x04" or (dados[:4] == b"PK\x05\x06")


def _linhas(texto):
    """Linhas para o diff. XML gravado numa linha só (WEG WPS 2.60 faz assim) é quebrado entre as tags,
    senão qualquer mudança vira "a linha inteira mudou"."""
    linhas = texto.splitlines()
    if texto.lstrip().startswith("<") and any(len(l) > 200 for l in linhas):
        return re.sub(r">\s*<", ">\n<", texto).splitlines()
    return linhas


def diff_texto(a, b, nome_a="a", nome_b="b"):
    linhas = list(difflib.unified_diff(_linhas(a), _linhas(b), nome_a, nome_b, lineterm="", n=3))
    adic = sum(1 for l in linhas if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in linhas if l.startswith("-") and not l.startswith("---"))
    cortado = len(linhas) > MAX_LINHAS
    return {"tipo": "texto", "linhas": linhas[:MAX_LINHAS], "adicionadas": adic, "removidas": rem, "cortado": cortado}


def regioes_binarias(a, b):
    """Faixas [inicio, fim) onde os bytes diferem, comparando posição a posição. A sobra do maior conta como região."""
    regioes = []
    n = min(len(a), len(b))
    for base in range(0, n, BLOCO):
        fim_bloco = min(base + BLOCO, n)
        if a[base:fim_bloco] == b[base:fim_bloco]:
            continue
        i = base
        while i < fim_bloco:
            if a[i] != b[i]:
                j = i
                while j < fim_bloco and a[j] != b[j]:
                    j += 1
                if regioes and i - regioes[-1][1] < JUNTAR:
                    regioes[-1][1] = j
                else:
                    regioes.append([i, j])
                i = j
            else:
                i += 1
    if len(a) != len(b):
        if regioes and n - regioes[-1][1] < JUNTAR:
            regioes[-1][1] = max(len(a), len(b))
        else:
            regioes.append([n, max(len(a), len(b))])
    return regioes


def diff_binario(a, b):
    regioes = regioes_binarias(a, b)
    return {"tipo": "binario", "tamanho_a": len(a), "tamanho_b": len(b), "hash_a": sha256(a), "hash_b": sha256(b),
            "bytes_alterados": sum(f - i for i, f in regioes),
            "regioes": [{"inicio": i, "fim": f} for i, f in regioes[:MAX_REGIOES]],
            "regioes_total": len(regioes)}


def _entradas(dados):
    with zipfile.ZipFile(io.BytesIO(dados)) as z:
        return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}


def diff_zip(a, b):
    try:
        ea, eb = _entradas(a), _entradas(b)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError):
        return diff_binario(a, b)       # ZIP cifrado ou estranho: cai para binário
    alteradas = []
    for nome in sorted(ea.keys() & eb.keys()):
        if ea[nome] == eb[nome]:
            continue
        alteradas.append({"nome": nome, **comparar_bytes(ea[nome], eb[nome], nome, nome, dentro_zip=True)})
    return {"tipo": "zip", "adicionadas": sorted(eb.keys() - ea.keys()), "removidas": sorted(ea.keys() - eb.keys()),
            "alteradas": alteradas, "iguais": sum(1 for n in ea.keys() & eb.keys() if ea[n] == eb[n])}


def comparar_bytes(a, b, nome_a="a", nome_b="b", dentro_zip=False):
    if a == b:
        return {"tipo": "igual", "tamanho": len(a)}
    if not dentro_zip and eh_zip(a) and eh_zip(b):
        return diff_zip(a, b)
    ta, tb = decodificar(a), decodificar(b)
    if ta is not None and tb is not None:
        return diff_texto(ta, tb, nome_a, nome_b)
    return diff_binario(a, b)


def comparar(acervo, id_a, id_b):
    va, vb = acervo.versao(id_a), acervo.versao(id_b)
    r = comparar_bytes(acervo.ler(va.hash), acervo.ler(vb.hash),
                       f"{va.arquivo} (versão {va.id})", f"{vb.arquivo} (versão {vb.id})")
    r["a"], r["b"] = va.dict(), vb.dict()
    return r


def _tam(n):
    for u in ("B", "KiB", "MiB"):
        if n < 1024 or u == "MiB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def em_texto(r, recuo=""):
    """Resultado de comparar() para o terminal."""
    out = []
    t = r["tipo"]
    if t == "igual":
        out.append(f"{recuo}conteúdo idêntico ({_tam(r['tamanho'])})")
    elif t == "texto":
        out.append(f"{recuo}texto: +{r['adicionadas']} / -{r['removidas']} linhas")
        out += [recuo + l for l in r["linhas"]]
        if r["cortado"]:
            out.append(f"{recuo}… diff cortado em {MAX_LINHAS} linhas")
    elif t == "binario":
        out.append(f"{recuo}binário: {_tam(r['tamanho_a'])} -> {_tam(r['tamanho_b'])}, "
                   f"{r['bytes_alterados']} byte(s) em {r['regioes_total']} região(ões)")
        out.append(f"{recuo}  sha256 {r['hash_a'][:16]} -> {r['hash_b'][:16]}")
        for g in r["regioes"]:
            out.append(f"{recuo}  0x{g['inicio']:08x}–0x{g['fim']:08x}  ({g['fim'] - g['inicio']} B)")
        if r["regioes_total"] > len(r["regioes"]):
            out.append(f"{recuo}  … mais {r['regioes_total'] - len(r['regioes'])} região(ões)")
    elif t == "zip":
        out.append(f"{recuo}zip: {len(r['adicionadas'])} adicionada(s), {len(r['removidas'])} removida(s), "
                   f"{len(r['alteradas'])} alterada(s), {r['iguais']} igual(is)")
        out += [f"{recuo}  + {n}" for n in r["adicionadas"]]
        out += [f"{recuo}  - {n}" for n in r["removidas"]]
        for e in r["alteradas"]:
            out.append(f"{recuo}  ~ {e['nome']}")
            out += em_texto(e, recuo + "    ")
    return out
