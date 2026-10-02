"""Scoring + message drafting with an LLM via an OpenAI-compatible API (local server or cloud provider).

Prompt ideas borrowed from: MietRadar (answer embedded questions, no generic opener, vary wording,
fixed-template mode so the model never touches your personal facts), isaksolheim/gesucht +
jonasdieker (code word / Stichwort, woven into a sentence; first only if the ad demands it), Fredy (scam signals).
"""

import json
import logging
import os
import re
from datetime import date

from openai import AsyncOpenAI

log = logging.getLogger("llm")

DE_WORDS = set(
    "und der die das ich wir ist nicht mit zimmer wohnung miete sind eine für auf bei uns du euch wohnen suchen gemeinsam küche".split()
)
EN_WORDS = set("the and we is are room flat you with for our looking rent apartment kitchen us live share".split())

# Cheap deterministic scam signals (Fredy scamSignals.js idea). Shown to you and to the model.
SCAM_PATTERNS = {
    r"western union|moneygram|paysafe": "asks for untraceable payment",
    r"vorkasse|kaution (vorab|im voraus) überweisen|deposit (in advance|before)": "wants money before viewing",
    r"schlüssel (per|mit der) post|keys? (by|via) (post|mail|courier)|airbnb.{0,40}(kaution|deposit)": "keys by post / fake booking platform",
    r"(bin|lebe|wohne|arbeite) (derzeit |zurzeit |gerade )?(im ausland|in (london|england|spanien|frankreich|nigeria))|currently (abroad|living in|working in) (london|uk|spain|france)": "owner abroad",
    r"(whats ?app|e-?mail) (mich|me) (direkt|directly)|schreib(t)? mir (eine )?e-?mail an": "moves contact off the platform",
}


def scam_signals(text: str) -> list[str]:
    low = text.lower()
    return [why for pat, why in SCAM_PATTERNS.items() if re.search(pat, low)]


def detect_language(text: str) -> str:
    low = text.lower()
    if re.search(r"english[- ]speaking|only english|in english|we speak english|auf englisch", low):
        return "en"
    toks = re.findall(r"[a-zäöüß]+", low)
    de = sum(t in DE_WORDS for t in toks)
    en = sum(t in EN_WORDS for t in toks)
    return "en" if en > de * 1.2 else "de"


def has_number(text: str, phone: str) -> bool:
    """True if every digit of `phone` appears in order in `text`, whatever the spacing ("+39 324…" / "0039324…")."""
    want = re.sub(r"\D", "", phone)
    return bool(want) and want.lstrip("0") in re.sub(r"\D", "", text)


# Sentences a template-mode part must never contain: a claim about where I live (the model mixes that up
# with the flat's location) or a refusal to answer. Dropping them is safe: the fixed text carries the message.
BAD_SENTENCE = re.compile(
    r"\bich wohne\b|\bda ich (in|im|am) [^.!?]{0,40}\bwohne\b|\bi (currently )?live (in|near|at)\b|"
    r"keine angaben|kann ich (leider )?nicht(s)? sagen|can'?t (say|tell|share)|no information",
    re.I,
)


def drop_bad_sentences(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(s for s in parts if not BAD_SENTENCE.search(s))


def first_name(poster: str) -> str:
    """'Anna Schmidt' -> 'Anna'; skips company-ish names and initials-only."""
    p = (poster or "").strip()
    if not p or re.search(r"gmbh|immobilien|wohnen|morgen|team|\d", p, re.I):
        return ""
    w = p.split()[0].strip(".")
    return w if len(w) > 1 and w[0].isupper() else ""


SYSTEM = """You help one person find a home in {city}. Ads are WG rooms or studios (TYPE says which).
Read the whole ad (all sections), judge the fit and write the first message.

Scoring (0-10): commute and location matter most (use the COMMUTE numbers, never guess), then
price/size, WG vibe or studio quality, availability date, and who they are looking for
(age/gender requirements I don't meet = low score). Scam signs = score 0-2.

Message rules:
- Write ONLY in {language}.
- Use ONLY facts from MY PROFILE. Never invent anything about me (age, job, hobbies, dates).
- First check the ad for a code word / Stichwort / instruction like "start your message with...",
  "mention the word...", "schreib 'Banane' in die Betreffzeile". If there is one, put it in "keyword"
  and work it into a natural, friendly sentence that makes sense with the word (e.g. Banane ->
  "Banane im Müsli ist übrigens mein Frühstücks-Geheimtipp."), never as a bare word on its own.
  If the ad says the message must START with it, that sentence opens the message with the code word
  as its very first word, on its own line before the greeting; otherwise put it wherever it fits best.
- If the ad asks applicants questions (e.g. "tell us your favourite dish", "what would you bring to
  a WG brunch?"), answer each one briefly. Harmless preferences may be made up, identity facts never.
- Start with something specific from the ad, not "I saw your ad" / "ich habe eure Anzeige gesehen".
- Greeting: CONTACT NAME if given ("Hallo Anna," / "Hi Anna,"). If no name: WG → "Hallo zusammen," /
  "Hi everyone,"; studio → "Guten Tag," / "Hello,". Never write a name that is not given.
- {register}
- Layout: greeting line, then 2-3 short paragraphs separated by blank lines, then the sign-off.
- No placeholders like [Name], no subject line, no emojis, no bullet lists.
{length_rule}

Return strict JSON only:
{{"score": <0-10 integer>,
  "reasons": "<one short English sentence: why this score>",
  "commute_ok": <true|false, is the commute feasible for me given the numbers>,
  "red_flags": "<scam signs or dealbreakers, English, or empty string>",
  "keyword": "<code word the ad asks for, or empty string>",
  "keyword_at_start": <true only if the ad says the message must begin with the code word>,
  "questions": "<questions in the ad that I answered, or empty string>",
  "notes": ["<2-5 short English notes for ME only (never sent): what stands out about the place, good or
             bad (furnished?, deposit, extra costs, limited duration, condition, floor...), and any test or
             trick hidden in the ad (code word, questions to answer, 'write in German', documents wanted)
             and how the message handles it>"],
  "message": "<the message>"}}"""

# Template mode: your own fixed message is sent word for word; the model only writes the small part that
# depends on the ad (it goes where {personal} is) and judges the ad.
SYSTEM_TEMPLATE = """You help one person find a home in {city}. Ads are WG rooms or studios (TYPE says which).
Read the whole ad (all sections) and judge the fit. The message itself is my own fixed text; you only
write the small part that depends on this ad, which goes into the middle of my text.

Scoring (0-10): commute and location matter most (use the COMMUTE numbers, never guess), then
price/size, WG vibe or studio quality, availability date, and who they are looking for
(age/gender requirements I don't meet = low score). Scam signs = score 0-2.

The part you write ("message"):
- Write ONLY in {language}, even if the ad or my profile uses another language. {register}
- One short, plain sentence (at most 25 words) saying why this place works for me, linking one concrete
  fact from the ad to one fact from MY PROFILE (e.g. close to my work, they cook together and I like
  cooking, an international WG). Write it the way a normal person types a quick message.
- MY FIXED TEXT is shown below: never repeat anything it already says (my age, job, origin, hobbies,
  cooking, smoking, pets, languages, viewing, contact). Add only what is new.
- Use ONLY facts from MY PROFILE. Never invent anything (study subject, where I live now, dates). The
  flat's location is not where I live: wrong "Da ich in der Altstadt wohne, ...", right "Von hier wäre
  ich schnell bei der Arbeit in ...". If the ad asks something my profile doesn't answer, leave it out
  (don't write that you can't answer) and say so in "notes".
- Never praise the flat or the ad, never repeat their description back to them, no adjectives like
  wonderful, perfect, charming, cosy, exciting (wunderbar, perfekt, spannend, gemütlich, charmant, toll).
- If the ad asks applicants questions or to mention something (favourite dish, hobbies, why you...),
  add one short, casual sentence per question with the answer from MY PROFILE, unless my fixed text
  already answers it.
- If the ad has a code word / Stichwort / instruction like "start your message with...", "mention the
  word...": put it in "keyword" and use the word in one short, natural sentence here (e.g. Banane ->
  "Banane im Müsli ist übrigens mein Frühstück."), never as a bare word.
- Nothing else: no greeting, no introduction of myself, no German level, no viewing or contact
  details, no sign-off. My fixed text already has all of that.

Return strict JSON only:
{{"score": <0-10 integer>,
  "reasons": "<one short English sentence: why this score>",
  "commute_ok": <true|false, is the commute feasible for me given the numbers>,
  "red_flags": "<scam signs or dealbreakers, English, or empty string>",
  "keyword": "<code word the ad asks for, or empty string>",
  "keyword_at_start": <true only if the ad says the message must begin with the code word>,
  "questions": "<questions in the ad that I answered, or empty string>",
  "notes": ["<2-5 short English notes for ME only (never sent): what stands out about the place, good or
             bad (furnished?, deposit, extra costs, limited duration, condition, floor...), and any test or
             trick hidden in the ad (code word, questions to answer, 'write in German', documents wanted),
             and anything the ad asks for that my fixed text doesn't cover>"],
  "message": "<the part you write>"}}"""

REGISTER = {
    "room": 'WG style: casual, "du"/"ihr" in German.',
    "studio": 'Studio: polite and a bit formal, "Sie" in German; stress reliability, stable job, documents.',
}


class LLM:
    def __init__(self, cfg: dict, me: dict, city: str = "München"):
        self.cfg = cfg
        self.me = me
        self.city = city
        # LLM_API_KEY in the environment wins over config.yaml; local servers ignore the key
        key = os.environ.get("LLM_API_KEY") or cfg.get("api_key") or "none"
        self.client = AsyncOpenAI(base_url=cfg["base_url"], api_key=key)

    def _profile(self):
        whatsapp = f"MY WHATSAPP: {self.me['whatsapp']}\n\n" if self.me.get("whatsapp") else ""
        guidelines = self.me["message_guidelines"]
        if self.template_mode:  # length and layout rules would pull the model back into writing a whole message
            guidelines = (
                "(Written for a whole message. For your small part, follow only the content rules: what never "
                "to mention, du/Sie, which facts to use. Ignore length, structure and closing rules.)\n" + guidelines
            )
        return (
            f"MY NAME: {self.me['name']}\n\n{whatsapp}MY PROFILE:\n{self.me['profile']}\n\n"
            f"MY PREFERENCES:\n{self.me['preferences']}\n\n"
            f"MESSAGE GUIDELINES:\n{guidelines}"
        )

    def _template(self, kind, lang):
        tpls = self.me.get("templates") or {}
        kind = kind if kind in ("room", "studio") else "room"
        return tpls.get(f"{kind}_{lang}") or tpls.get(f"room_{lang}") or tpls.get(lang) or ""

    def with_contact(self, msg: str) -> str:
        """Safety net: a model can drop or garble a digit, so the number is checked digit by digit."""
        phone = str(self.me.get("whatsapp") or "").strip()
        if not phone or has_number(msg, phone):
            return msg
        return f"{msg.rstrip()}\nWhatsApp: {phone}"

    @staticmethod
    def _ad(l, commute_text=""):
        kind = (
            "STUDIO / 1-room apartment (I'd live alone; write to the landlord or current tenant)"
            if l.kind == "studio"
            else "Room in a shared flat (WG)"
        )
        name = first_name(l.poster)
        return (
            f"TODAY: {date.today():%d.%m.%Y}\nTYPE: {kind}\nTITLE: {l.title}\n"
            f"CONTACT NAME: {name or 'unknown - do not use a name'}\n"
            f"RENT: {l.rent} €  SIZE: {l.size} m²  ADDRESS: {l.address or l.street + ', ' + l.district}\n"
            f"AVAILABLE: {l.available_from} – {l.available_to or 'open-ended'}\n"
            f"FLATMATES: {l.flatmates or '?'}   LOOKING FOR: {l.wanted or '?'}\n"
            f"POSTER MEMBER SINCE: {l.member_since or '?'}   AD ONLINE: {l.online or '?'}\n"
            f"{commute_text}\n"
            f"KEY FACTS: {l.details}\n\nAD TEXT:\n{l.description}"
        )

    @property
    def template_mode(self):
        return self.me.get("mode", "full") == "template"

    def _system(self, lang, kind):
        language = "German" if lang == "de" else "English"
        register = REGISTER.get(kind, REGISTER["room"])
        if self.template_mode:
            return SYSTEM_TEMPLATE.format(city=self.city, language=language, register=register)
        return SYSTEM.format(
            city=self.city, language=language, register=register, length_rule="Length: follow MESSAGE GUIDELINES."
        )

    async def _chat(self, system, user, want_json=True):
        if self.cfg.get("no_think_suffix", True):
            user += "\n/no_think"
        kw = dict(
            model=self.cfg["model"],
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if (temp := self.cfg.get("temperature", 0.6)) is not None:  # null: some models only take their default
            kw["temperature"] = temp
        if self.cfg.get("extra_body"):
            kw["extra_body"] = self.cfg["extra_body"]
        if want_json:
            try:
                r = await self.client.chat.completions.create(response_format={"type": "json_object"}, **kw)
            except Exception as e:  # server without JSON mode
                log.debug("json mode failed (%s), retrying plain", e)
                r = await self.client.chat.completions.create(**kw)
        else:
            r = await self.client.chat.completions.create(**kw)
        out = r.choices[0].message.content or ""
        return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()

    @staticmethod
    def _json(s):
        try:
            return json.loads(s)
        except Exception:
            m = re.search(r"\{.*\}", s, re.S)
            try:
                return json.loads(m.group(0)) if m else {}
            except Exception:
                return {}

    def _wrap_template(self, l, lang, body, keyword, keyword_at_start=False):
        """My fixed text with the ad-specific part in {personal}. Placeholders: {greeting} {personal} {name}
        {whatsapp}; anything else in the text stays exactly as written."""
        kind = l.kind if l.kind in ("room", "studio") else "room"
        tpl = self._template(kind, lang)
        if not tpl:
            log.warning("template mode, but no me.templates.%s_%s: sending only the model's part", kind, lang)
            return body
        name = first_name(l.poster)
        greet = {
            ("de", "room"): f"Hallo {name}," if name else "Hallo zusammen,",
            ("en", "room"): f"Hi {name}," if name else "Hi everyone,",
            ("de", "studio"): f"Hallo {name}," if name else "Guten Tag,",
            ("en", "studio"): f"Hello {name}," if name else "Hello,",
        }[(lang, kind)]
        body = body.strip()
        first = ""
        if keyword and keyword_at_start:  # the ad wants the code word first: its sentence opens the message
            parts = re.split(r"(?<=[.!?])\s+", body)
            hit = next((s for s in parts if keyword.lower() in s.lower()), "")
            if hit:
                first, body = hit, " ".join(s for s in parts if s is not hit)
        fill = {"greeting": greet, "personal": body, "name": self.me["name"], "whatsapp": self.me.get("whatsapp", "")}
        msg = tpl
        for k, v in fill.items():
            msg = msg.replace("{" + k + "}", str(v or ""))
        msg = re.sub(r"[ \t]+\n", "\n", msg)
        msg = re.sub(r"\n{3,}", "\n\n", msg).strip()  # an empty {personal} leaves no gap
        if first and re.match(r"(hallo|hi|hey|servus|moin|grüß|liebe|dear|hello)\b", first, re.I):
            msg = msg.replace(greet, first, 1)  # the code phrase is itself a greeting ("Servus Corps RP!")
        elif first:
            msg = f"{first}\n\n{msg}"
        if keyword and keyword.lower() not in msg.lower():
            msg = f"{keyword}\n\n{msg}"
        return msg

    async def evaluate(self, listing, lang: str, commute_text="") -> dict:
        language = "German" if lang == "de" else "English"
        user = self._profile()
        if self.template_mode and (tpl := self._template(listing.kind, lang)):
            user += f"\n\n=== MY FIXED TEXT (your part goes where {{personal}} is) ===\n{tpl}"
        user += "\n\n=== AD ===\n" + self._ad(listing, commute_text)
        if self.template_mode:
            user += f'\n\nWrite "message" in {language}.'
        raw = await self._chat(self._system(lang, listing.kind), user)
        d = self._json(raw)
        try:
            d["score"] = max(0, min(10, int(d.get("score", 0))))
        except (TypeError, ValueError):
            d["score"] = 0
        for k in ("reasons", "red_flags", "keyword", "questions"):
            d[k] = str(d.get(k) or "").strip()
        notes = d.get("notes") or []
        notes = [notes] if isinstance(notes, str) else notes
        d["notes"] = [str(n).strip() for n in notes if str(n).strip()][:6]
        d["keyword_at_start"] = bool(d["keyword"]) and str(d.get("keyword_at_start")).lower() == "true"
        msg = (d.get("message") or "").strip()
        if self.template_mode:  # the part can be short or even empty: the fixed text carries the message
            if len(msg.split()) >= 5 and detect_language(msg) != lang:  # wrote in the ad's other language
                msg = await self._chat(
                    f"Translate into {language}. Keep names and any code word unchanged. Output only the translation.",
                    msg,
                    want_json=False,
                )
            msg = drop_bad_sentences(msg)
            if (kw := d["keyword"]) and kw.lower() not in msg.lower():  # forgot the code word: one sentence for it
                extra = await self._chat(
                    f"Write one short, natural {language} sentence that uses the word or phrase given, unchanged, "
                    "the way a person would mention it in a casual message. Output only the sentence.",
                    kw,
                    want_json=False,
                )
                msg = f"{msg} {extra.strip()}".strip()
            d["message"] = self.with_contact(
                self._wrap_template(listing, lang, msg, d["keyword"], d["keyword_at_start"])
            )
            return self._flag_scams(listing, d)
        if len(msg.split()) < 8:
            # it rejected the ad (e.g. day rentals only) and wrote nothing: that's a low score, not an error
            if d["score"] <= 3:
                d["message"] = ""
                return d
            raise ValueError(f"LLM returned no usable message: {raw[:300]}")
        if (kw := d["keyword"]) and kw.lower() not in msg.lower():  # model forgot the code word: one retry
            where = "as the very first word of the message" if d["keyword_at_start"] else "wherever it fits"
            msg = (
                await self.rewrite(
                    listing,
                    lang,
                    msg,
                    f'The ad asks for the code word "{kw}". Work it '
                    f"into a natural sentence, {where}. Change nothing else.",
                    commute_text,
                )
            ).strip()
            if kw.lower() not in msg.lower():
                msg = f"{kw}\n\n{msg}"  # last resort: never lose the code word (the card flags it)
        d["message"] = self.with_contact(msg)
        return self._flag_scams(listing, d)

    @staticmethod
    def _flag_scams(listing, d):
        flags = scam_signals(listing.title + " " + listing.description)
        if flags:
            d["red_flags"] = "; ".join(filter(None, [d["red_flags"]] + flags))
            d["score"] = min(d["score"], 2)
        return d

    async def rewrite(self, listing, lang: str, previous: str, feedback: str, commute_text="") -> str:
        language = "German" if lang == "de" else "English"
        fresh = feedback.strip() in ("", ".")
        if self.template_mode:  # my fixed text stays; only what I ask for changes
            how = (
                "Change only what WHAT TO CHANGE asks for and keep every other sentence word for word: most "
                "of the draft is my own fixed text. Plain words, no praise of the flat. "
            )
            if fresh:
                feedback = "Rephrase only the sentences about this specific ad; keep everything else word for word."
        else:
            how = ""
            if fresh:
                feedback = "Write a fresh, different version."
        system = (
            f"Rewrite a message answering a flat/WG ad. Write ONLY in {language}. "
            f"{REGISTER.get(listing.kind, REGISTER['room'])} {how}Use only facts from the profile; keep any "
            "code word and my WhatsApp number from the previous draft. Output only the new message text, nothing else."
        )
        user = (
            self._profile()
            + "\n\n=== AD ===\n"
            + self._ad(listing, commute_text)
            + f"\n\n=== PREVIOUS DRAFT ===\n{previous}\n\n=== WHAT TO CHANGE ===\n{feedback}"
        )
        return self.with_contact(await self._chat(system, user, want_json=False))

    async def translate(self, text: str, keyword: str = "") -> str:
        """English version of a German draft, only shown to me in Telegram (never sent)."""
        system = (
            "Translate the German message into natural English. Keep the exact meaning and the "
            "paragraph breaks; add, drop or explain nothing. Leave names unchanged"
            + (f', and keep the code word "{keyword}" exactly as it is (do not translate it)' if keyword else "")
            + ". Output only the translation."
        )
        return await self._chat(system, text, want_json=False)
