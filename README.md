# The Founder's Dream — YouTube → WordPress Auto Blog

हर दिन **सुबह 9 बजे और शाम 7 बजे (IST)** एक YouTube वीडियो उठाता है, उसका transcript निकालता है, founder की details निकालता है, और **thefoundersdream.in** पर English + Hindi दोनों में SEO-optimised blog post publish कर देता है — thumbnail, schema markup और internal links के साथ।

कोई manual step नहीं। कोई server नहीं। GitHub Actions पर free चलता है।

---

## यह हर run में क्या करता है

```
1. YouTube Data API से channel के videos की list
2. Shorts/clips filter out (5 मिनट से छोटे, #businesspodcast वाले)
3. जो अभी तक process नहीं हुआ — नया पहले, वरना backlog से सबसे पुराना
4. Transcript निकालो (3 fallback layers)
5. Claude से guest founder का नाम, company, role, topics extract करो
6. Claude से पूरी blog post — English में, फिर Hindi में
7. YouTube thumbnail → WordPress media library (featured image + alt text)
8. Video embed + FAQ + JSON-LD schema जोड़कर assemble
9. दोनों posts WordPress पर publish, आपस में hreflang से linked
10. state/processed.json में record, repo में commit — दोबारा वही video नहीं
```

**आउटपुट:** रोज़ 2 वीडियो × 2 भाषाएँ = **4 posts/दिन**.
सिर्फ़ 2 posts/दिन चाहिए तो `config.yaml` में `languages:` से एक भाषा हटा दें।

---

## Setup — एक बार, ~30 मिनट

### Step 1 — WordPress mu-plugin upload करें

`wp-mu-plugin/tfd-rest-meta.php` को अपने server पर यहाँ डालें:

```
wp-content/mu-plugins/tfd-rest-meta.php
```

`mu-plugins` folder न हो तो बना लें। Activate करने की ज़रूरत नहीं — mu-plugins अपने आप चलते हैं।

**यह क्यों चाहिए:** Rank Math के SEO fields (title, description, focus keyword) WordPress REST API से by default नहीं लिखे जा सकते। यह file उन्हें unlock करती है, और duplicate-post detection भी इसी से काम करता है।

> इसके बिना भी automation चलेगा — बस Rank Math meta खाली रहेगा और dedupe सिर्फ़ slug पर होगा।

### Step 2 — WordPress Application Password बनाएँ

1. WP Admin → **Users → Profile**
2. नीचे **Application Passwords**
3. नाम: `TFD Automation` → **Add New**
4. जो password दिखे उसे copy कर लें (दोबारा नहीं दिखेगा)

> यह आपका असली login password नहीं है। कभी भी revoke किया जा सकता है।

### Step 3 — YouTube Data API key

1. [console.cloud.google.com](https://console.cloud.google.com) → नया project
2. **APIs & Services → Library → YouTube Data API v3 → Enable**
3. **Credentials → Create Credentials → API key**
4. (सुझाव) key को YouTube Data API v3 तक restrict कर दें

Free quota 10,000 units/day है — हमारा use ~200/day। बहुत काफ़ी।

### Step 4 — Anthropic API key

[console.anthropic.com](https://console.anthropic.com) → **API Keys** → Create Key → billing add करें।

**अनुमानित खर्च:** Sonnet पर ~₹8–15 प्रति post → 4 posts/दिन ≈ **₹1,200–1,800/महीना**।

### Step 5 — GitHub repo बनाएँ

1. GitHub पर नया **private** repo: `tfd-blog-automation`
2. इस folder की सारी files upload करें (`.env` छोड़कर — वो कभी commit नहीं होती)
3. **Settings → Secrets and variables → Actions → New repository secret** में ये 4 डालें:

| Secret | Value |
|---|---|
| `YOUTUBE_API_KEY` | Step 3 वाली key |
| `ANTHROPIC_API_KEY` | Step 4 वाली key |
| `WP_USERNAME` | आपका WordPress username (email नहीं) |
| `WP_APP_PASSWORD` | Step 2 वाला application password |
| `TRANSCRIPT_PROXY` | *(optional, नीचे पढ़ें)* |

4. **Settings → Actions → General → Workflow permissions** → **Read and write permissions** चुनें
   *(state file commit करने के लिए ज़रूरी है)*

### Step 6 — पहला test

GitHub → **Actions → TFD auto blog → Run workflow**:

- `dry_run` = **true**
- `count` = **1**

Run खत्म होने पर नीचे **Artifacts → post-previews** download करें। दोनों HTML files पढ़ें।

पसंद आए तो `dry_run` = **false** से दोबारा चलाएँ — पहली असली post live हो जाएगी।

उसके बाद cron अपने आप रोज़ सुबह-शाम चलेगा।

---

## रोज़मर्रा का इस्तेमाल

**Keywords बदलने हैं?** सिर्फ़ `keywords.yaml` edit करके commit कर दें। Code को हाथ लगाने की ज़रूरत नहीं।

**कोई ख़ास वीडियो अभी publish करना है?**
Actions → Run workflow → `video_id` में YouTube ID डालें (URL के `v=` के बाद वाला हिस्सा)।

**Backlog जल्दी clear करना है?**
Run workflow → `count` = `5`. (एक साथ 10 से ज़्यादा न करें — Google को sudden content dump पसंद नहीं।)

**कुछ दिन रोकना है?** Actions tab → workflow → `···` → **Disable workflow**.

**Time बदलना है?** `.github/workflows/publish.yml` में cron edit करें। याद रखें UTC है:

```
IST time = UTC + 5:30
09:00 IST → cron "30 3 * * *"
19:00 IST → cron "30 13 * * *"
```

---

## Transcript के बारे में ज़रूरी बात

यह पूरे system का सबसे नाज़ुक हिस्सा है। YouTube datacenter IPs (जिन पर GitHub Actions चलता है) को अक्सर block करता है, तो transcript fetch fail हो सकता है।

Code में 3 layers हैं:

1. `youtube-transcript-api` — सबसे तेज़, free
2. `yt-dlp` auto-subtitles — layer 1 fail होने पर
3. दोनों fail → **वीडियो skip** (thin content publish करने से बेहतर है कुछ न करना)

**अगर बार-बार fail हो:** एक residential proxy लें ([Webshare](https://www.webshare.io) का "Residential" plan ~$6/महीना) और उसका URL `TRANSCRIPT_PROXY` secret में डाल दें:

```
http://username:password@p.webshare.io:80
```

State file में हर failure record होता है। 3 बार fail होने के बाद वो वीडियो अपने आप skip होने लगता है, ताकि pipeline अटके नहीं।

---

## Local testing (optional)

```bash
pip install -r requirements.txt
cp .env.example .env        # keys भरें
python -m src.main --check                   # connections test
python -m src.main --dry-run                 # publish किए बिना preview
python -m src.main --video-id ABC123 --dry-run
python -m src.main                           # असली publish
```

---

## Files

```
config.yaml                      सारी settings — भाषाएँ, filters, timing
keywords.yaml                    SEO keyword strategy (आप इसे edit करते रहेंगे)
requirements.txt
.env.example
.github/workflows/publish.yml    दिन में 2 बार का cron
wp-mu-plugin/tfd-rest-meta.php   WordPress side का छोटा plugin
state/processed.json             कौन से videos हो चुके (auto-commit)
src/
  config.py          settings + secrets
  youtube_client.py  video fetch + Shorts filter + selection logic
  transcripts.py     3-layer transcript extraction
  generator.py       Claude prompts — founder extraction + post writing
  render.py          embed + FAQ + JSON-LD schema assembly
  wordpress_client.py REST API — media, tags, posts, dedupe
  state.py           processed/failed tracking
  main.py            orchestrator + CLI
```

---

## Duplicate posts नहीं बनेंगे — 3 guards

1. `state/processed.json` हर run के बाद repo में commit होता है
2. Publish से पहले WordPress में `tfd_youtube_id` meta search होता है
3. Slug collision पर अपने आप `-2`, `-3` लगता है

साथ ही `concurrency` group की वजह से दो runs कभी एक साथ नहीं चलेंगे।

---

## Quality guardrails

- Transcript 300 शब्दों से कम → वीडियो skip
- Shorts और clips filter out
- हर claim transcript से ही — prompt में explicitly मना किया गया है कि numbers, dates या quotes बनाए
- Host (Abhishek Vyas) को guest समझने से रोका गया है
- AI-slop phrases की banned list `keywords.yaml` में है
- हर post में 2+ internal links और FAQ schema
