"""
Retrieval delle fonti da Europe PMC.

Europe PMC ha API REST aperte e gratuite (nessuna chiave richiesta) e copre
PubMed/MEDLINE piu' full-text open access. Perfetta per recuperare titolo,
abstract, anno, PMID/DOI.

Doc: https://europepmc.org/RestfulWebService
"""

from __future__ import annotations

import logging
import httpx

log = logging.getLogger(__name__)

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF_WORKS = "https://api.crossref.org/works"

# Europe PMC marca la provenienza di ogni record nel campo "source":
#   MED = MEDLINE/PubMed (peer-reviewed), PMC = PubMed Central full-text,
#   PPR = PREPRINT (NON peer-reviewed), AGR/CBA/ETH/... = altre fonti.
_PREPRINT_SOURCE = "PPR"

# LIVELLO 0 - Cochrane: le revisioni sistematiche Cochrane sono il gold standard
_COCHRANE_FILTER = 'JOURNAL:"Cochrane Database Syst Rev"'

# Filtro Europe PMC per le fonti che SINTETIZZANO l'evidenza
_SYNTHESIS_FILTER = (
    '(PUB_TYPE:"systematic review" OR PUB_TYPE:"Meta-Analysis" '
    'OR PUB_TYPE:"Guideline" OR PUB_TYPE:"Practice Guideline" '
    'OR PUB_TYPE:"Review")'
)

# Tipi di pubblicazione che alziamo in classifica
_PRIORITY_KEYWORDS = (
    "meta-analysis",
    "systematic review",
    "randomized",
    "randomised",
    "cohort",
)


def _evidence_bonus(result: dict, is_cochrane: bool = False) -> int:
    """
    Bonus secondo la PIRAMIDE DELLE EVIDENZE.
    Cochrane > meta-analisi/systematic review > RCT > coorte > resto.
    """
    if is_cochrane:
        return 8
    pubtype = ""
    if isinstance(result.get("pubTypeList"), dict):
        pubtype = " ".join(result["pubTypeList"].get("pubType", [])).lower()
    title = (result.get("title") or "").lower()
    hay = f"{title} {pubtype}"
    if "meta-analysis" in hay or "systematic review" in hay:
        return 6
    if "randomized" in hay or "randomised" in hay or "controlled trial" in hay:
        return 4
    if "cohort" in hay:
        return 2
    if "review" in hay:
        return 1
    return 0


def _score(result: dict, search_terms: list[str] | None = None,
           is_cochrane: bool = False) -> int:
    """
    Punteggio per ordinare le fonti.
    1. PERTINENZA (dominante): +10 per ogni termine presente in titolo+abstract.
    2. FORZA DI EVIDENZA (tie-breaker forte): piramide delle evidenze.
    3. RECENZA (tie-breaker leggero): +1 se >= 2015.
    """
    score = 0
    title = (result.get("title") or "").lower()
    abstract = (result.get("abstractText") or "").lower()
    haystack_text = f"{title} {abstract}"

    if search_terms:
        for term in search_terms:
            t = term.strip().lower()
            if t and t in haystack_text:
                score += 10

    score += _evidence_bonus(result, is_cochrane)

    try:
        if int(result.get("pubYear", "0")) >= 2015:
            score += 1
    except (ValueError, TypeError):
        pass

    if not result.get("abstractText"):
        score -= 5
    return score


def _do_query(full_query: str, max_results: int, timeout: float) -> list[dict]:
    """Esegue una singola query su Europe PMC con payload ottimizzati per Telegram."""
    # OTTIMIZZAZIONE TELEGRAM: Limitiamo la dimensione della pagina a un valore snello (max 12)
    # invece di moltiplicare indiscriminatamente, riducendo drasticamente il consumo di RAM.
    page_size_ottimizzato = min(max(max_results * 2, 10), 12)

    params = {
        "query": full_query,
        "format": "json",
        "resultType": "core",     # 'core' include abstractText
        "pageSize": page_size_ottimizzato,
        "sort": "CITED desc",     # proxy di rilevanza temporaneo prima del riordinamento interno
    }

    print(f"[evidence] QUERY -> {full_query!r}")
    try:
        resp = httpx.get(EUROPEPMC_SEARCH, params=params, timeout=timeout)
        print(f"[evidence] HTTP {resp.status_code} url={resp.url}")
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Europe PMC query fallita per %r: %s", full_query, exc)
        print(f"[evidence] ERRORE: {exc}")
        return []

    results = data.get("resultList", {}).get("result", [])
    print(f"[evidence] {len(results)} risultati grezzi (hitCount totale: "
          f"{data.get('hitCount', '?')})")
    return results


def _term_hitcount(term: str, extra_clause: str, timeout: float) -> int:
    """
    Quanti record ha QUESTO termine da solo (con lo stesso filtro extra_clause).
    Serve a distinguere termini "rari" (specifici e preziosi: GNMT, sarcosine)
    da termini "comuni" (generici e sacrificabili: liver, dietary). Una sola
    chiamata leggera con pageSize=1: ci interessa solo l'hitCount.
    """
    params = {"query": f"({term}){extra_clause}", "format": "json", "pageSize": 1}
    try:
        resp = httpx.get(EUROPEPMC_SEARCH, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("hitCount", 10**9)
    except (httpx.HTTPError, ValueError):
        return 10**9  # in dubbio, trattalo come generico (non privilegiarlo)


def _order_by_rarity(terms: list[str], extra_clause: str, timeout: float) -> list[str]:
    """
    Riordina i termini dal piu' raro (hitCount basso, es. "GNMT") al piu' comune
    (hitCount alto, es. "liver"). I termini rari sono quelli specifici che una
    AND con troppi altri termini rischia di "strozzare" a zero risultati, ma
    che presi da soli o in coppia trovano proprio le fonti giuste.
    """
    scored = [(t, _term_hitcount(t, extra_clause, timeout)) for t in terms]
    scored.sort(key=lambda x: x[1])
    print(f"[evidence] rarita' termini: {[(t, h) for t, h in scored]}")
    return [t for t, _ in scored]


def _progressive_search(terms: list[str], extra_clause: str, max_results: int,
                        timeout: float) -> tuple[list[dict], list[str]]:
    """
    1) Prova tutti i termini in AND, poi togliendo l'ultimo finche' trova
       risultati (fallback "dal generale al particolare").
    2) Se non trova NULLA cosi' (capita quando ci sono 5-6 termini che
       mischiano nomi tecnici di nicchia con parole generiche), riordina i
       termini per RARITA' (hitCount) e riprova con i 3 e poi i 2 piu' rari:
       tiene i termini specifici (GNMT, sarcosine) che un troncamento "ai
       primi N" rischierebbe di scartare per caso, e lascia andare quelli
       generici (dietary, liver) che strozzano l'intersezione.
    """
    for n in range(len(terms), 1, -1):
        subset = terms[:n]
        full_query = f"({' AND '.join(subset)}){extra_clause}"
        results = _do_query(full_query, max_results, timeout)
        if results:
            return results, subset
        print(f"[evidence] 0 risultati con {n} parole, riprovo con meno")

    if len(terms) > 3:
        print("[evidence] === fallback per rarita' dei termini ===")
        by_rarity = _order_by_rarity(terms, extra_clause, timeout)
        for n in (3, 2):
            subset = by_rarity[:n]
            full_query = f"({' AND '.join(subset)}){extra_clause}"
            results = _do_query(full_query, max_results, timeout)
            if results:
                return results, subset
            print(f"[evidence] 0 risultati con i {n} termini piu' rari, riprovo")

    return [], terms


def search_evidence(search_terms: list[str], max_results: int = 4,
                    timeout: float = 20.0, include_preprints: bool = False) -> list[dict]:
    """
    Cerca su Europe PMC e restituisce una lista di dict normalizzati (Pool Unico).
    """
    print(f"[evidence] search_terms ricevuti: {search_terms!r}")
    terms = [t.strip() for t in search_terms if t.strip()]
    if not terms:
        print("[evidence] nessun termine di ricerca, salto la query")
        return []

    preprint_clause = "" if include_preprints else f" NOT SRC:{_PREPRINT_SOURCE}"
    base_clause = f" AND HAS_ABSTRACT:y{preprint_clause}"

    pool: dict[str, dict] = {}        # pmid -> record grezzo (dedup)
    cochrane_pmids: set[str] = set()  # quali pmid vengono da Cochrane
    used_terms = terms

    # Raccolgo da tutti i livelli nello stesso pool
    print("[evidence] === raccolta Cochrane ===")
    coch, t0 = _progressive_search(terms, f"{base_clause} AND {_COCHRANE_FILTER}",
                                   max_results, timeout)
    for r in coch:
        pmid = r.get("pmid") or r.get("id")
        if pmid:
            pool[pmid] = r
            cochrane_pmids.add(pmid)
    if coch:
        used_terms = t0

    print("[evidence] === raccolta sintesi (review/meta-analisi/guideline) ===")
    synth, t1 = _progressive_search(terms, f"{base_clause} AND {_SYNTHESIS_FILTER}",
                                    max_results, timeout)
    for r in synth:
        pmid = r.get("pmid") or r.get("id")
        if pmid and pmid not in pool:
            pool[pmid] = r
    if synth and not coch:
        used_terms = t1

    # Studi primari solo se il pool e' ancora vuoto o magro
    if len(pool) < max_results:
        print("[evidence] === raccolta studi primari (pool ancora magro) ===")
        prim, t2 = _progressive_search(terms, base_clause, max_results, timeout)
        for r in prim:
            pmid = r.get("pmid") or r.get("id")
            if pmid and pmid not in pool:
                pool[pmid] = r
        if prim and not coch and not synth:
            used_terms = t2

    if not pool:
        print("[evidence] nessun risultato a nessun livello")
        return []

    # Ordino il pool unito secondo algoritmo del TAR (Pertinenza + Piramide Evidenze)
    candidates = list(pool.values())
    candidates.sort(
        key=lambda r: _score(r, used_terms,
                             is_cochrane=(r.get("pmid") or r.get("id")) in cochrane_pmids),
        reverse=True,
    )

    normalized: list[dict] = []
    for r in candidates:
        abstract = (r.get("abstractText") or "").strip()
        if not abstract:
            print(f"[evidence] scarto (no abstract): {r.get('title', '')[:60]!r}")
            continue

        haystack = f"{r.get('title','')} {abstract}".lower()
        if used_terms and not any(t.lower() in haystack for t in used_terms):
            print(f"[evidence] scarto (0 termini pertinenti): "
                  f"PMID={r.get('pmid')} {r.get('title','')[:50]!r}")
            continue

        pmid = r.get("pmid") or r.get("id") or "N/A"
        r_is_cochrane = pmid in cochrane_pmids
        src = (r.get("source") or "").upper()
        is_preprint = src == _PREPRINT_SOURCE

        pubtype = ""
        if isinstance(r.get("pubTypeList"), dict):
            types = r["pubTypeList"].get("pubType", [])
            pubtype = ", ".join(types) if isinstance(types, list) else str(types)

        normalized.append({
            "pmid": pmid,
            "doi": r.get("doi", ""),
            "title": (r.get("title") or "").strip().rstrip("."),
            "abstract": abstract[:3000],
            "year": r.get("pubYear", "s.d."),
            "source": ("Cochrane systematic review" if r_is_cochrane
                       else pubtype or ("preprint NON peer-reviewed" if is_preprint else "articolo")),
            "is_preprint": is_preprint,
            "is_cochrane": r_is_cochrane,
        })
        print(f"[evidence] ACCETTO PMID={pmid} cochrane={r_is_cochrane} "
              f"score={_score(r, used_terms, r_is_cochrane)} pubtype={pubtype[:40]!r}")
        if len(normalized) >= max_results:
            break

    log.info("Europe PMC: %d fonti per parole %r", len(normalized), used_terms)
    print(f"[evidence] -> {len(normalized)} fonti finali (pool: {len(pool)}, "
          f"parole usate: {used_terms})\n")
    return normalized


# ---------------------------------------------------------------------------
# RISOLUZIONE DI UNA FONTE CITATA (per la modalita' "verifica articolo")
# ---------------------------------------------------------------------------
def _clean_abstract(text: str) -> str:
    """Rimuove tag JATS/HTML grezzi dagli abstract Crossref."""
    import re
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _crossref_by_doi(doi: str, timeout: float) -> dict | None:
    url = f"{CROSSREF_WORKS}/{doi.strip()}"
    print(f"[resolve] Crossref DOI -> {doi!r}")
    try:
        resp = httpx.get(url, timeout=timeout,
                         headers={"User-Agent": "Contraddittorio/1.0 (mailto:user@example.com)"})
        resp.raise_for_status()
        item = resp.json().get("message", {})
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[resolve] Crossref DOI fallito: {exc}")
        return None
    return {
        "title": (item.get("title") or ["N/D"])[0],
        "abstract": _clean_abstract(item.get("abstract", "")) or "(abstract non disponibile su Crossref)",
        "year": str((item.get("issued", {}).get("date-parts", [[None]]) or [[None]])[0][0] or "s.d."),
        "source": (item.get("container-title") or ["articolo"])[0],
        "doi": item.get("DOI", doi),
    }


def _crossref_by_title(title: str, timeout: float) -> dict | None:
    print(f"[resolve] Crossref titolo -> {title[:60]!r}")
    try:
        resp = httpx.get(CROSSREF_WORKS, timeout=timeout,
                         params={"query.bibliographic": title, "rows": 3},
                         headers={"User-Agent": "Contraddittorio/1.0 (mailto:user@example.com)"})
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[resolve] Crossref titolo fallito: {exc}")
        return None
    if not items:
        return None
    item = items[0]
    return {
        "title": (item.get("title") or ["N/D"])[0],
        "abstract": _clean_abstract(item.get("abstract", "")) or "(abstract non disponibile su Crossref)",
        "year": str((item.get("issued", {}).get("date-parts", [[None]]) or [[None]])[0][0] or "s.d."),
        "source": (item.get("container-title") or ["articolo"])[0],
        "doi": item.get("DOI", ""),
    }


def resolve_article(ref_type: str, riferimento: str, search_terms: list[str],
                    timeout: float = 20.0) -> dict | None:
    """
    Risolve un riferimento (DOI/titolo/descrizione) in un articolo reale.
    """
    ref = (riferimento or "").strip()
    print(f"[resolve] tipo={ref_type} riferimento={ref[:80]!r}")

    if ref_type == "doi" and ref:
        art = _crossref_by_doi(ref, timeout)
        if art:
            return art

    if ref:
        epmc = search_evidence([ref], max_results=1, timeout=timeout)
        if epmc:
            print("[resolve] trovato via Europe PMC")
            return epmc[0]

    if ref:
        art = _crossref_by_title(ref, timeout)
        if art and art.get("abstract", "").startswith("(abstract") is False:
            print("[resolve] trovato via Crossref (titolo)")
            return art
        if art:
            return art

    if search_terms:
        epmc = search_evidence(search_terms, max_results=1, timeout=timeout)
        if epmc:
            print("[resolve] trovato via Europe PMC (parole chiave)")
            return epmc[0]

    print("[resolve] fonte non risolta")
    return None
