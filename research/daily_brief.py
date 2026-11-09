import feedparser, requests, datetime, urllib.parse, pathlib

TOPICS = {
    "LIMS - pathology lab software (India)": "LIMS software India OR pathology lab software India OR diagnostic lab software India",
    "InventoryCloud - inventory and billing software (India)": "inventory billing software India OR retail billing software India OR GST billing software small business",
    "HMS - hospital management systems (India)": "hospital management system India OR HMS software India OR hospital software ABDM India",
}
MODEL = "qwen2.5:3b"
OLLAMA = "http://127.0.0.1:11434/api/generate"
OUT = pathlib.Path(r"D:\vault\outputs\reports")

def news(query, n=8):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
    return [(e.title, e.link) for e in feedparser.parse(url).entries[:n]]

def summarize(topic, items):
    heads = "\n".join("- " + t for t, _ in items)
    prompt = ("Topic: " + topic + "\nHeadlines:\n" + heads +
              "\n\nWrite 3-5 short bullet points on what matters for a software company selling to small businesses and clinics in Bihar, India. "
              "Use only these headlines. If they are unclear, say so.")
    r = requests.post(OLLAMA, json={"model": MODEL, "prompt": prompt, "stream": False,
                                    "options": {"num_ctx": 2048}}, timeout=900)
    return r.json()["response"].strip()

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    lines = ["# Daily research brief - " + today, ""]
    for topic, query in TOPICS.items():
        items = news(query)
        lines += ["## " + topic, ""]
        if not items:
            lines += ["No news found.", ""]
            continue
        lines += [summarize(topic, items), "", "Sources:"]
        lines += ["- [" + t + "](" + u + ")" for t, u in items] + [""]
    (OUT / (today + ".md")).write_text("\n".join(lines), encoding="utf-8")
    print("Saved", OUT / (today + ".md"))

main()
