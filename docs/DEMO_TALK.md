# Demo talk: about 3½ minutes

A script to read aloud while presenting. **Bold** lines in brackets are what to do on screen; everything else is spoken. Timings are approximate, and the spoken part is about 480 words.

**Before you start**
- Server running: `uvicorn analyst.api:app --port 8000`.
- http://localhost:8000 open, signed in as **Meera Rao · exec**. Click *Start a new conversation* so the thread is empty.
- Window about 1280 px wide, so the sidebar and the answer card both fit.
- Don't click **Run eval suite** live: it makes dozens of model calls. Show `evals/REPORT.md` instead (last section).

---

## 1. The problem and the idea (0:00–0:45)

> Every business has people asking questions like "why did our payment success rate drop?" Today that means waiting for an analyst. Large language models can write SQL, so the obvious answer is to let the model answer.
>
> The trouble is that a model will happily give you a confident number it made up. It may also query data you aren't allowed to see, or pick one definition of "success rate" when there are four.
>
> So the core idea of this system is: **the model proposes, code decides.** The model reads the question, and it writes the final wording. Everything in between is deterministic code:
> - which data you're allowed to see;
> - whether a query is safe to run;
> - every number, and the analysis behind it.
>
> And before an answer is shown, every number in it is checked against the actual query results.

## 2. The main question (0:45–2:00)

**[Click the example "Why did our payment success rate drop in the last two weeks?"]**

> I'm signed in as an executive. Let me ask the classic question.

**[The clarification card appears]**

> First, it doesn't guess. "Success rate" has four definitions here, and they give different answers: 85.5%, 82.8%, 93% and 92%. So it asks, and it shows each one's value so I can see why it matters. I'll take the default.

**[Click Continue. The progress chips stream: understanding, access check, running queries…]**

> While it works, you can watch each stage live: understanding the question, checking my access, then running about twenty queries. Every one goes through a SQL safety gate before it touches the warehouse.

**[The answer appears. Point at the headline.]**

> And here's the answer. Success rate fell 2.6 points. But the important part is *why*: not because anything got worse. Traffic shifted toward UPI Collect, a payment flow with a lower success rate, and almost all that extra traffic came from one merchant.

**[Click the Charts tab, point at the waterfall]**

> The waterfall shows it: almost all of the drop is a mix effect, and almost none is a rate effect. A naive dashboard would have told you "UPI is failing" and sent the team chasing the wrong problem.

**[Back to the Answer tab; point at the green, amber and blue blocks]**

> Facts are in green. Hypotheses are in amber, labelled as hypotheses, with how to check them. The recommendation, in blue, has an impact estimate: move that merchant to UPI intent, worth about two points.

**[Click a fact's `s1` link, which opens Evidence]**

> And every fact links to the exact SQL that produced it, with the rows and the data version. Nothing here is unverifiable.

## 3. When things go wrong (2:00–3:05)

**[Switch the user to Kabir Shah · ops_analyst. Ask "Show me MDR revenue by merchant last month"]**

> Now the failure cases. An operations analyst asks for revenue data they aren't cleared for. It's refused before any SQL is written, and they're told what they *can* see instead.

**[Switch to Neha Joshi · regional_manager_west. Click "Top 5 merchants by GMV last month", then open Evidence]**

> A regional manager for the West only sees West data. She doesn't have to ask for that, and the model can't forget it. The access rule is injected into every query by the safety gate, and you can see it right here in the SQL: `region = 'WEST'`.

**[Type "Delete the duplicates from Oct 5"]**

> And if anyone tries to change data, or slip in a prompt injection, it's rejected outright. This system can only read.

## 4. How we know it works (3:05–3:30)

**[Open `evals/REPORT.md`, or the README results table]**

> Finally, how do we know the answers are right? The data has planted anomalies with known answers, and an evaluation harness runs twenty of those cases end to end. It checks the numbers against ground truth, compares results with hand-written SQL, and uses a second model as a judge. The rule-based backend passes all twenty. A real model, Gemini, passes nineteen. And testing on real model output is what found the bugs I just showed you being prevented.
>
> So: **the model is great at language, and code makes sure it's never wrong about the numbers.** Thank you.

---

## If something goes off-script

- **A step is slow:** free models can take 10–30 s. Keep talking over the progress chips. The script's waits are already filled.
- **The footer says "worded by offline (fallback)":** the free model hit its rate limit, and the rule-based backend answered that step instead. Say: *"The free model just hit its rate limit, so the deterministic fallback answered. Same numbers, plainer wording. That's the design working."*
- **Running short on time:** drop the West-manager step. **Have spare time:** ask *"What happened to card success on September 10?"* It finds a Meridian Bank outage from 18:00 to 23:00, with an hour-by-hour chart.
- **Model usage:** the whole talk is roughly 8–10 calls to OpenRouter. Free models allow a limited number of requests per day, so rehearse once at most.
