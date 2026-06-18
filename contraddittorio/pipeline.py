"""
Pipeline: dal messaggio grezzo al contraddittorio formattato.

    messaggio -> estrai claim -> per ogni claim {retrieval Europe PMC -> analisi grounded}
              -> formatta in testo leggibile per Telegram
"""

from __future__ import annotations

import asyncio
import logging

from . import analyze, evidence

log = logging.getLogger(__name__)

_VERDICT_LABEL = {
    "smentito": "Smentito",
    "solo_animali_invitro": "Solo su animali / in vitro",
    "ipotesi_plausibile": "Ipotesi plausibile",
    "confermato": "Confermato",
    "non_studiato": "Non studiato / non verificabile",
    "supportata": "Confermato",
    "parzialmente_supportata": "Ipotesi plausibile",
    "non_supportata": "Non studiato / non verificabile",
    "contraddetta": "Smentito",
    "evidenza_insufficiente": "Non studiato / non verificabile",
}

_VERDICT_EMOJI = {
    "smentito": "🔴",
    "solo_animali_invitro": "🟠",
    "ipotesi_plausibile": "🟡",
    "confermato": "🟢",
    "non_studiato": "⚪",
    "supportata": "🟢",
    "parzialmente_supportata": "🟡",
    "non_supportata": "⚪",
    "contraddetta": "🔴",
    "evidenza_insufficiente": "⚪",
}

_TIPO_LABEL = {
    "clinica": "claim clinica/causale",
    "biochimica_base": "meccanismo biochimico di base",
}


def _verify_cited_source(message_text: str, model: str) -> str:
    """
    STEP 0: se il messaggio cita una fonte specifica, la recupera e confronta
    cio' che il post le attribuisce con cio' che la fonte dice davvero.
    Ritorna un blocco di testo da anteporre al contraddittorio, o "" se non
    c'e' una fonte citata.
    """
    det = analyze.detect_cited_source(message_text, model)
    if not det["ha_fonte"]:
        print("[pipeline] nessuna fonte citata rilevata")
        return ""

    print(f"[pipeline] fonte citata: tipo={det['tipo_riferimento']} "
          f"rif={det['riferimento'][:60]!r}")
    article = evidence.resolve_article(
        det["tipo_riferimento"], det["riferimento"], det["termini_ricerca_en"])

    if not article:
        return (
            "*Fonte citata nel messaggio*\n"
            f"Il messaggio si appoggia a una fonte specifica ({det['riferimento']}), "
            "ma non sono riuscito a recuperarla automaticamente per confrontarla. "
            "Verdetto sulle singole affermazioni qui sotto.\n"
        )

    cmp = analyze.compare_source(det["tesi_attribuita"], article, model)
    pmid = article.get("pmid", "")
    link = (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid and pmid != "N/A"
            else (f"https://doi.org/{article.get('doi')}" if article.get("doi") else ""))
    titolo = article.get("title", "N/D")
    titolo_link = f"[{titolo}]({link})" if link else titolo

    icon = "\u2705" if cmp["fedele"] else "\u274c"
    block = ["*Verifica della fonte citata*",
             f"Fonte reale: {titolo_link} ({article.get('year','s.d.')})"]
    if cmp["corrispondenza"] == "approssimativa":
        block.append("_(fonte piu' vicina trovata, corrispondenza approssimativa)_")
    if cmp.get("cosa_dice_davvero"):
        block.append(f"Cosa dice davvero: {cmp['cosa_dice_davvero']}")
    if cmp["fedele"]:
        block.append(f"{icon} Il messaggio rappresenta la fonte in modo sostanzialmente fedele.")
    else:
        block.append(f"{icon} Travisamento: {cmp['travisamento']}")
    return "\n".join(block) + "\n"


async def _validate_claim_mechanism(claim_it: str, model: str) -> dict:
    """Valida se il meccanismo biochimico nella claim è realmente corretto."""
    try:
        validation = analyze.validate_mechanism(claim_it, claim_it, model)
        return validation
    except Exception as e:
        print(f"[pipeline] validazione biochimica fallita: {e}")
        return {"valido": True, "spiegazione": ""}  # fallback: assumi valido


async def _analyze_single_claim(i: int, c: dict, model: str) -> tuple[int, str, str]:
    """Analizza una singola claim in modo asincrono."""
    claim_it = c["claim_it"]
    
    # STEP 1: Validazione biochimica (cattura meccanismi inventati)
    if c.get("tipo") == "biochimica_base":
        validation = await _validate_claim_mechanism(claim_it, model)
        if not validation["valido"]:
            print(f"[pipeline] MECCANISMO FALSO RILEVATO: {claim_it}")
            emoji = _VERDICT_EMOJI.get("smentito", "")
            block = [f"
*{i}. {claim_it}*"]
            block.append(f"_{c.get('tipo', '')}_")
            block.append(f"{emoji} Verdetto: Smentito")
            block.append(f"Motivo: {validation['spiegazione']}")
            verdetto_riassunto = f"- \"{claim_it}\" -> Smentito (meccanismo falso)"
            return i, "
".join(block), verdetto_riassunto
    
    # STEP 2: Retrieval normale
    candidates = evidence.search_evidence(c["search_terms_en"], max_results=3)
    
    if len(candidates) > 2:
        abstracts = analyze.rerank_candidates(claim_it, candidates, model)[:4]
    else:
        abstracts = candidates[:4]
    
    result = analyze.analyze_claim(claim_it, abstracts, model)
    
    emoji = _VERDICT_EMOJI.get(result["verdict"], "")
    label = _VERDICT_LABEL.get(result["verdict"], result["verdict"])
    tipo_label = _TIPO_LABEL.get(c.get("tipo"), "")
    
    block = [f"\n*{i}. {claim_it}*"]
    if tipo_label:
        block.append(f"_{tipo_label}_")
    block.append(f"{emoji} Verdetto: {label}")
    
    distanza = result.get("distanza_dalle_fonti")
    if distanza is not None:
        block.append(f"Distanza dalle fonti: {distanza}/4")
    
    if result.get("livello_evidenza"):
        block.append(f"Evidenza: {result['livello_evidenza']}")
    if result.get("nucleo_vero"):
        block.append(f"Cosa e' vero: {result['nucleo_vero']}")
    if result.get("dove_salta"):
        block.append(f"Dove salta: {result['dove_salta']}")
    
    cites = result.get("citazioni", [])
    if cites:
        block.append("Fonti:")
        for cit in cites[:4]:
            pmid = cit.get("pmid", "N/A")
            rel = cit.get("rilevanza", "")
            link = (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                    if pmid not in ("N/A", "", None) else "")
            block.append(f"  - [{pmid}]({link}) {rel}" if link else f"  - {rel}")
    else:
        block.append("Fonti: nessun abstract pertinente recuperato (verdetto da prendere con cautela).")
    
    verdetto_riassunto = f"- \"{claim_it}\" -> {label}"
    return i, "\n".join(block), verdetto_riassunto


def analyze_message(message_text: str, model: str, max_claims: int = 3) -> str:
    """Esegue l'intera pipeline e ritorna una stringa formattata (Markdown Telegram)."""
    source_block = _verify_cited_source(message_text, model)
    
    claims = analyze.extract_claims(message_text, model)
    
    if not claims:
        base = (
            "Non ho individuato affermazioni scientifiche verificabili in questo "
            "messaggio. Se contiene solo opinioni o esortazioni, non c'e' molto da "
            "controllare contro la letteratura."
        )
        return (source_block + "\n" + base) if source_block else base
    
    claims.sort(key=lambda c: (-int(c.get("centralita", 2)),
                               0 if c.get("tipo") == "clinica" else 1))
    claims = claims[:max_claims]
    
    # Parallelizza l'analisi delle claim (tutte insieme, non in sequenza)
    async def run_analysis():
        tasks = [_analyze_single_claim(i, c, model) 
                 for i, c in enumerate(claims, 1)]
        return await asyncio.gather(*tasks)
    
    results = asyncio.run(run_analysis())
    
    blocks: list[str] = []
    if source_block:
        blocks.append(source_block)
    blocks.append(
        f"Ho isolato {len(claims)} affermazione/i verificabile/i e le ho confrontate "
        f"con la letteratura su Europe PMC.\n"
    )
    
    verdetti_riepilogo: list[str] = []
    for _, claim_block, verdetto in sorted(results, key=lambda x: x[0]):
        blocks.append(claim_block)
        verdetti_riepilogo.append(verdetto)
    
    patterns = analyze.detect_patterns(message_text, "\n".join(verdetti_riepilogo), model)
    if patterns:
        pblock = ["\n*Perche' un messaggio cosi' convince*",
                  "Lo scetticismo e' sano: ecco i passaggi che rendono questo messaggio "
                  "persuasivo e su cui vale la pena fermarsi a riflettere."]
        for p in patterns:
            pblock.append(f"\u2022 _{p['nome']}_: {p['come']}")
        blocks.append("\n".join(pblock))
    
    blocks.append(
        "\n---\nNota: questo e' un contraddittorio automatico basato sugli abstract "
        "recuperati, non un parere medico. Verifica sempre le fonti citate."
    )
    return "\n".join(blocks)
