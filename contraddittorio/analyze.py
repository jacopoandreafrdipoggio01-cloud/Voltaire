"""
Chiamate al modello LLM (Anthropic) per i due passaggi di ragionamento:
  1) estrarre le claim dal messaggio sospetto
  2) confrontare ogni claim con gli abstract recuperati

Entrambi gli step chiedono JSON puro e lo parsano in modo difensivo.
"""

from __future__ import annotations

import json
import logging
import re

from anthropic import Anthropic

from . import prompts

log = logging.getLogger(__name__)

# Modello: vedi config. Tieni un modello capace per l'analisi grounded.
_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # legge ANTHROPIC_API_KEY dall'ambiente
    return _client


def _extract_json(text: str) -> dict:
    """
    Estrae il primo oggetto JSON dal testo del modello.
    I modelli a volte aggiungono backtick o preamboli nonostante le istruzioni:
    qui ripuliamo in modo difensivo.
    """
    text = text.strip()
    # Rimuovi eventuali fence ```json ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: prendi la prima graffa bilanciata
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    log.error("Impossibile parsare JSON dal modello: %.300s", text)
    return {}


def _call(system: str, user: str, model: str, max_tokens: int = 2000) -> str:
    resp = _get_client().messages.create(
    model=model,
    max_tokens=max_tokens,
    temperature=0,
    messages=[{"role": "user", "content": user}],
    system=system,
)       
    # Concatena i blocchi di testo
    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )


def extract_claims(message_text: str, model: str) -> list[dict]:
    """Ritorna una lista di {claim_it, tipo, search_terms_en}."""
    raw = _call(prompts.CLAIM_EXTRACTION_SYSTEM, message_text, model, max_tokens=4000)
    data = _extract_json(raw)
    claims = data.get("claims", [])
    # Filtro difensivo sullo schema
    out = []
    for c in claims:
        if isinstance(c, dict) and c.get("claim_it"):
            tipo = c.get("tipo", "clinica")
            if tipo not in ("clinica", "biochimica_base"):
                tipo = "clinica"  # default prudente: tratta come centrale
            terms = c.get("search_terms_en", []) or []
            print(f"[analyze] claim={c['claim_it'][:70]!r} tipo={tipo} "
                  f"search_terms_en={terms!r}")
            out.append({
                "claim_it": c["claim_it"],
                "tipo": tipo,
                "search_terms_en": terms,
            })
    return out


def analyze_claim(claim_it: str, abstracts: list[dict], model: str) -> dict:
    """Confronta una claim con gli abstract recuperati. Ritorna il dict-verdetto."""
    user = prompts.build_analysis_user_prompt(claim_it, abstracts)
    raw = _call(prompts.ANALYSIS_SYSTEM, user, model, max_tokens=1500)
    data = _extract_json(raw)

    verdict = data.get("verdict", "evidenza_insufficiente")

    # distanza_dalle_fonti: intero 1-4, o None per evidenza_insufficiente.
    # Validazione difensiva: qualunque valore fuori schema -> None.
    distanza = data.get("distanza_dalle_fonti")
    if verdict == "evidenza_insufficiente":
        distanza = None
    else:
        try:
            distanza = int(distanza)
            if not 1 <= distanza <= 4:
                distanza = None
        except (TypeError, ValueError):
            distanza = None

    # Valori di default difensivi
    return {
        "verdict": verdict,
        "distanza_dalle_fonti": distanza,
        "nucleo_vero": data.get("nucleo_vero", ""),
        "dove_salta": data.get("dove_salta", ""),
        "livello_evidenza": data.get("livello_evidenza", ""),
        "citazioni": data.get("citazioni", []) or [],
    }


def detect_cited_source(message_text: str, model: str) -> dict:
    """Rileva se il messaggio cita una fonte specifica. Ritorna il dict di rilevamento."""
    raw = _call(prompts.SOURCE_DETECT_SYSTEM, message_text, model, max_tokens=800)
    data = _extract_json(raw)
    return {
        "ha_fonte": bool(data.get("ha_fonte", False)),
        "tipo_riferimento": data.get("tipo_riferimento"),
        "riferimento": data.get("riferimento", "") or "",
        "tesi_attribuita": data.get("tesi_attribuita", "") or "",
        "termini_ricerca_en": data.get("termini_ricerca_en", []) or [],
    }


def compare_source(tesi_attribuita: str, article: dict, model: str) -> dict:
    """Confronta cosa il post sostiene con cio' che la fonte dice davvero."""
    user = prompts.build_source_compare_prompt(tesi_attribuita, article)
    raw = _call(prompts.SOURCE_COMPARE_SYSTEM, user, model, max_tokens=1200)
    data = _extract_json(raw)
    return {
        "fonte_trovata": bool(data.get("fonte_trovata", True)),
        "corrispondenza": data.get("corrispondenza", "approssimativa"),
        "cosa_dice_davvero": data.get("cosa_dice_davvero", ""),
        "travisamento": data.get("travisamento", ""),
        "fedele": bool(data.get("fedele", False)),
    }


def rerank_candidates(claim_it: str, candidates: list[dict], model: str) -> list[dict]:
    """
    Filtro semantico: fa scegliere all'LLM quali candidati sono davvero pertinenti
    alla claim, scartando le fonti agganciate solo per una parola in comune.
    Se qualcosa va storto, restituisce i candidati invariati (fail-safe).
    """
    if not candidates:
        return candidates
    user = prompts.build_rerank_prompt(claim_it, candidates)
    try:
        raw = _call(prompts.RERANK_SYSTEM, user, model, max_tokens=300)
        data = _extract_json(raw)
        idxs = data.get("pertinenti", [])
        kept = [candidates[i] for i in idxs if isinstance(i, int) and 0 <= i < len(candidates)]
        print(f"[rerank] {len(candidates)} candidati -> {len(kept)} pertinenti (indici {idxs})")
        # se il rerank scarta tutto ma avevamo candidati, teniamo i primi 2 grezzi
        # per non lasciare la claim senza fonti per un errore del filtro
        return kept if kept else candidates[:2]
    except Exception as exc:
        print(f"[rerank] errore, tengo i candidati grezzi: {exc}")
        return candidates


def detect_patterns(message_text: str, verdetti_riassunto: str, model: str) -> list[dict]:
    """
    Identifica i meccanismi retorici di travisamento in uso nel messaggio,
    scegliendoli dal catalogo. Ritorna lista di {id, nome, come}.
    Fail-safe: in caso di errore ritorna lista vuota (il blocco viene omesso).
    """
    # mappa id -> nome leggibile dal catalogo
    nomi = {pid: desc.split(":")[0] for pid, desc in prompts.PATTERN_CATALOG}
    user = prompts.build_pattern_prompt(message_text, verdetti_riassunto)
    try:
        raw = _call(prompts.PATTERN_DETECT_SYSTEM, user, model, max_tokens=900)
        data = _extract_json(raw)
        out = []
        for p in data.get("pattern", []):
            pid = p.get("id", "")
            if pid in nomi:
                out.append({"id": pid, "nome": nomi[pid], "come": p.get("come", "")})
        print(f"[pattern] rilevati: {[p['id'] for p in out]}")
        return out
    except Exception as exc:
        print(f"[pattern] errore, salto: {exc}")
        return []
