
import os
import re
import json
import time
import html
import requests
import feedparser
import unicodedata
from bs4 import BeautifulSoup

# =========================
# CONFIGURATION
# =========================

FEEDS = [
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.theguardian.com/world/rss",
    "https://feeds.npr.org/1001/rss.xml",
]

KEYWORDS = re.compile(
    r"\b(trump|white house|pentagon|congress|senate|u\.s\.|"
    r"united states|american|washington|war|ukraine|russia|"
    r"gaza|israel|iran|hamas|nato|missile|airstrike|ceasefire|"
    r"troops|military|china|taiwan)\b",
    re.I
)

MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-lite-latest"]

MAX_POSTS = int(os.getenv("MAX_POSTS", "1"))
DRAFT = os.getenv("DRAFT", "true").lower() == "true"

# Optional in-app settings (edited from the Blog Bot Setup app, saved as
# settings.json in the repo). Anything set there overrides the defaults above.
try:
    with open("settings.json", encoding="utf-8") as _f:
        _settings = json.load(_f)
    if isinstance(_settings.get("feeds"), list) and _settings["feeds"]:
        FEEDS = [str(u) for u in _settings["feeds"]]
    if _settings.get("keywords"):
        KEYWORDS = re.compile(
            r"\b(" + "|".join(re.escape(str(k)) for k in _settings["keywords"]) + r")\b",
            re.I,
        )
    if _settings.get("max_posts"):
        MAX_POSTS = int(_settings["max_posts"])
    if "draft" in _settings:
        DRAFT = bool(_settings["draft"])
except FileNotFoundError:
    pass
except Exception as _e:
    print("settings.json ignored:", _e)

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; MohinTechNewsBot/1.0)"
}


# =========================
# DUPLICATE CHECK
# =========================

def load_posted():
    try:
        with open("posted.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_posted(posted):
    with open("posted.json", "w", encoding="utf-8") as f:
        json.dump(posted[-500:], f, ensure_ascii=False, indent=2)


# =========================
# SEO SLUG SUGGESTION
# =========================

def make_slug(title):
    title = unicodedata.normalize("NFKD", title)
    title = title.encode("ascii", "ignore").decode("ascii")
    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", "-", title)
    title = title.strip("-")
    return title[:80].strip("-") or "news-update"


# =========================
# COLLECT NEWS
# =========================

def candidates(posted):
    out = []
    seen = set(posted)

    for url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=UA["User-Agent"])
        except Exception as ex:
            print("Feed error:", url, str(ex)[:150])
            continue

        for entry in feed.entries[:15]:
            link = entry.get("link", "")
            headline = entry.get("title", "")
            summary = entry.get("summary", "")
            combined = headline + " " + summary

            if (
                link
                and link not in seen
                and KEYWORDS.search(combined)
            ):
                out.append(entry)
                seen.add(link)

    out.sort(
        key=lambda e: e.get("published_parsed") or time.gmtime(0),
        reverse=True
    )

    return out


# =========================
# EXTRACT SOURCE TEXT
# =========================

def article_text(link, fallback):
    try:
        response = requests.get(link, headers=UA, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup([
            "script", "style", "nav", "footer",
            "header", "aside", "noscript"
        ]):
            tag.decompose()

        paragraphs = [
            p.get_text(" ", strip=True)
            for p in soup.find_all("p")
        ]

        text = " ".join(
            p for p in paragraphs if len(p) > 60
        )

        if len(text) > 400:
            return text[:10000]

    except Exception as ex:
        print("Article extraction warning:", str(ex)[:150])

    return BeautifulSoup(
        fallback or "", "html.parser"
    ).get_text(" ", strip=True)[:6000]


# =========================
# GEMINI AI
# =========================

PROMPT = """
You are a careful professional news editor for MOHIN TECH BD.

Rewrite the supplied source into an original English news article.

STRICT RULES:
- Use ONLY facts supported by the source text.
- Never invent dates, names, quotes, numbers, background facts or claims.
- Do not copy long phrases from the source.
- Use neutral, factual, non-sensational language.
- Aim for 500-700 words only if the source supports that length.
- If the source is too short, write a shorter article instead of inventing facts.
- Use only these HTML tags in the article: <h2>, <p>, <ul>, <li>, <a>.
- Do not use <h1>, Markdown, CSS, or JavaScript.
- Do not include the source article's full text.
- SEO: choose ONE main keyword phrase for this story (the phrase a reader would type into Google, e.g. "Trump Schumer Senate majority leader").
- Create an SEO title that STARTS with the main keyword phrase, 50-60 characters, maximum 70 characters.
- Create a factual search description that includes the main keyword phrase, maximum 150 characters.
- Use the main keyword phrase in the FIRST paragraph of the article.
- Use variations of the main keyword phrase in the <h2> subheadings.
- Create 3-4 short, relevant labels, keyword-based, including the main keyword phrase or its core words.
- INTERNAL LINKS: the end of this prompt lists recent posts already published on this blog. Where it fits naturally, link 2-3 of them inside the article text with <a href="EXACT_URL">anchor text</a>, using ONLY the URLs from that list, copied exactly. Never invent a URL. If none of the listed posts is related to this story, include no links.
- Create image_query with the specific subject of this story: the key people, places, organizations or objects, in 3-6 English words, so a real photo of the actual subject can be found. Good: "Zelenskyy Kyiv press conference", "Hebron West Bank old city", "Federal Reserve headquarters Washington". Bad: "politics government building".
- Include people's names in image_query when they are central to the story.
- The article must be readable and well structured.

Return valid JSON only, with exactly these keys:
title, description, labels, image_query, html

The html value must contain the article body only.
Do not add commentary or Markdown code fences.

Source headline:
{title}

Source text:
{text}

Recent posts already published on this blog (title and URL — for internal links only):
{recent_posts}
"""


def ask_gemini(prompt):
    key = os.getenv("GEMINI_API_KEY")

    if not key:
        print("ERROR: GEMINI_API_KEY secret is missing")
        return None

    for model in MODELS:
        try:
            response = requests.post(
                "https://generativelanguage.googleapis.com/"
                f"v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": key,
                    "Content-Type": "application/json"
                },
                json={
                    "contents": [{
                        "parts": [{"text": prompt}]
                    }],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.3
                    }
                },
                timeout=120
            )

            if response.status_code != 200:
                print(
                    "Gemini error:",
                    model,
                    response.status_code,
                    response.text[:250]
                )
                continue

            result = response.json()
            text = (
                result["candidates"][0]["content"]["parts"][0]["text"]
            )

            data = json.loads(text)

            if not isinstance(data, dict):
                continue

            if not data.get("title") or not data.get("html"):
                continue

            return data

        except Exception as ex:
            print("Gemini request/parse error:", model, str(ex)[:200])

    return None


# =========================
# PIXABAY IMAGE SEARCH
# =========================

def _wikimedia_image(query):
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": (query or "news") + " filetype:bitmap",
                "gsrnamespace": 6,
                "gsrlimit": 20,
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata",
                "iiurlwidth": 1280
            },
            headers={
                "User-Agent": "MohinTechBot/1.0 (https://mohintechbd.blogspot.com)"
            },
            timeout=25
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        skip_words = ("map of", "locator", "logo", "coat of arms", "seal", "emblem", "diagram", "flag of")
        for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
            page_title = (page.get("title") or "").lower()
            if any(word in page_title for word in skip_words):
                continue
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            width = info.get("width", 0)
            height = info.get("height", 0)
            if width < 1280 or height <= 0 or width < height:
                continue
            meta = info.get("extmetadata") or {}
            license_name = (meta.get("LicenseShortName") or {}).get("value", "")
            if not (license_name.startswith("CC") or "Public domain" in license_name):
                continue
            image_url = info.get("thumburl") or info.get("url", "")
            if not image_url:
                continue
            artist = (meta.get("Artist") or {}).get("value", "")
            artist = re.sub(r"<[^>]+>", "", artist).strip()[:80]
            alt_text = html.escape(query or "News photo", quote=True)
            image_html = (
                f'<p><img src="{html.escape(image_url, quote=True)}" alt="{alt_text}" '
                'style="max-width:100%;height:auto"/></p>'
            )
            credit = artist or "Wikimedia Commons"
            image_html += (
                f'<p><small><i>Photo: {html.escape(credit)} / Wikimedia Commons '
                f'({html.escape(license_name)})</i></small></p>'
            )
            return image_html
        print("Image warning: no suitable Wikimedia photo found")
        return ""
    except Exception as ex:
        print("Wikimedia error:", str(ex)[:250])
        return ""


def _pixabay_image(query):
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        print("Image warning: PIXABAY_API_KEY is missing")
        return ""
    try:
        response = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": key,
                "q": query or "news",
                "image_type": "photo",
                "orientation": "horizontal",
                "safesearch": "true",
                "order": "popular",
                "min_width": 1600,
                "per_page": 20
            },
            timeout=25
        )
        response.raise_for_status()
        photos = response.json().get("hits", [])
        if not photos:
            print("Image warning: no Pixabay photos found")
            return ""
        photo = max(photos, key=lambda p: (p.get("imageWidth", 0), p.get("downloads", 0)))
        image_url = html.escape(photo.get("largeImageURL", ""), quote=True)
        page_url = html.escape(photo.get("pageURL", ""), quote=True)
        alt_text = html.escape(query or "News photo", quote=True)
        if not image_url:
            return ""
        image_html = (
            f'<p><img src="{image_url}" alt="{alt_text}" '
            'style="max-width:100%;height:auto"/></p>'
        )
        if page_url:
            image_html += (
                '<p><small>Image source: '
                f'<a href="{page_url}" rel="nofollow noopener">'
                'Pixabay</a></small></p>'
            )
        return image_html
    except Exception as ex:
        print("Pixabay error:", str(ex)[:250])
        return ""


def find_image(query, title=""):
    image_html = _wikimedia_image(query)
    if image_html:
        return image_html
    if title and title.strip().lower() != (query or "").strip().lower():
        image_html = _wikimedia_image(title)
        if image_html:
            return image_html
    return _pixabay_image(query)


# =========================
# BLOGGER OAUTH
# =========================

def blogger_token():
    required = [
        "CLIENT_ID",
        "CLIENT_SECRET",
        "REFRESH_TOKEN"
    ]

    missing = [name for name in required if not os.getenv(name)]

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["CLIENT_ID"],
            "client_secret": os.environ["CLIENT_SECRET"],
            "refresh_token": os.environ["REFRESH_TOKEN"],
            "grant_type": "refresh_token"
        },
        timeout=30
    )

    if not response.ok:
        print("OAuth error:", response.status_code, response.text[:500])
        response.raise_for_status()

    token = response.json().get("access_token")

    if not token:
        raise RuntimeError("OAuth response has no access token")

    return token


# =========================
# CREATE BLOGGER POST
# =========================

def publish(token, post):
    blog_id = os.getenv("BLOG_ID")

    if not blog_id:
        raise RuntimeError("BLOG_ID secret is missing")

    response = requests.post(
        f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts",
        params={"isDraft": str(DRAFT).lower()},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json=post,
        timeout=60
    )

    print("Blogger status:", response.status_code)

    if not response.ok:
        print("Blogger error:", response.text[:500])
        return False

    result = response.json()
    print("Post created:", result.get("url", "(URL not returned)"))
    print("Post ID:", result.get("id", "(ID not returned)"))

    return True


# =========================
# RECENT BLOG POSTS (for internal links)
# =========================

def recent_blog_posts(limit=10):
    try:
        response = requests.get(
            "https://mohintechbd.blogspot.com/feeds/posts/default",
            params={"alt": "json", "max-results": limit},
            headers=UA,
            timeout=20
        )
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
        lines = []
        for entry in entries:
            title = (entry.get("title", {}).get("$t") or "").strip()
            url = ""
            for link in entry.get("link", []):
                if link.get("rel") == "alternate":
                    url = link.get("href", "")
                    break
            if title and url:
                lines.append("- " + title + " \u2014 " + url)
        return "\n".join(lines) if lines else "(none available)"
    except Exception as ex:
        print("Recent posts fetch warning:", str(ex)[:150])
        return "(none available)"


# =========================
# MAIN BOT
# =========================

def main():
    posted = load_posted()
    recent_posts = recent_blog_posts()
    done = 0
    tried = 0

    token = blogger_token()

    for entry in candidates(posted):
        if done >= MAX_POSTS or tried >= 5:
            break

        tried += 1

        link = entry.get("link", "")
        headline = entry.get("title", "News update")
        source_summary = entry.get("summary", "")

        print("Processing:", headline)

        source = article_text(link, source_summary)

        if len(source) < 150:
            print("Skipped: source text is too short")
            continue

        prompt = PROMPT.format(
            title=headline,
            text=source,
            recent_posts=recent_posts
        )

        data = ask_gemini(prompt)

        if not data:
            print("Skipped: Gemini did not return valid article data")
            continue

        title = str(data.get("title", "")).strip()[:70]
        description = re.sub(
            r"\s+", " ", str(data.get("description", ""))
        ).strip()[:150]

        labels = data.get("labels", [])
        if not isinstance(labels, list):
            labels = []

        labels = list(dict.fromkeys(
            str(label).strip()[:50]
            for label in labels
            if str(label).strip()
        ))[:4]

        image_query = str(
            data.get("image_query", "news")
        ).strip()[:100]

        article_html = str(data.get("html", "")).strip()

        if not title or not article_html:
            print("Skipped: title or article body is empty")
            continue

        # Basic HTML cleanup for allowed article tags
        soup = BeautifulSoup(article_html, "html.parser")

        for tag in soup.find_all(True):
            if tag.name not in {"h2", "p", "ul", "li", "a"}:
                tag.unwrap()
            elif tag.name == "a":
                href = tag.get("href", "")
                if href.startswith("https://mohintechbd.blogspot.com/"):
                    tag.attrs = {"href": href}
                else:
                    tag.unwrap()
            else:
                tag.attrs = {}

        article_html = str(soup)

        # Approximate word count
        plain_text = BeautifulSoup(
            article_html, "html.parser"
        ).get_text(" ", strip=True)

        word_count = len(plain_text.split())
        print("Article word count:", word_count)

        if word_count < 500:
            print(
                "Warning: article is under 500 words. "
                "It will not be padded with invented facts."
            )

        # Generate an English slug suggestion.
        # Blogger API does not provide a documented custom permalink
        # field in the posts.insert request.
        suggested_slug = make_slug(title)
        print("Suggested permalink slug:", suggested_slug)

        # Create article image and source link.
        image_html = find_image(image_query, title)

        source_html = (
            '<p><em>Source: '
            f'<a href="{html.escape(link, quote=True)}" '
            'rel="nofollow noopener">'
            f'{html.escape(headline)}</a></em></p>'
        )

        body = image_html + article_html + source_html

        # Store description and slug suggestion as metadata for reference.
        # This does NOT set Blogger's Search Description or permalink.
        metadata = json.dumps({
            "search_description_suggestion": description,
            "suggested_slug": suggested_slug
        }, ensure_ascii=False)

        post = {
            "kind": "blogger#post",
            "title": title,
            "content": body,
            "labels": labels,
            "customMetaData": metadata
        }

        print("SEO title:", title)
        print("Search description suggestion:", description)
        print("Labels:", ", ".join(labels) or "(none)")
        print("Image query:", image_query)

        if publish(token, post):
            posted.append(link)
            save_posted(posted)
            done += 1

    print("Finished. Posts created:", done)
    print("Draft mode:", DRAFT)


if __name__ == "__main__":
    main()
