para corrigir o erro RPC
$ notebooklm use 5a83e6e6
Matched: 5a83e6e6-810... (Guia do Aplicativo Smart Força de Vendas)
08:31:05 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.325s
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ ID       ┃ Title                                           ┃ Owner ┃ Created ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ 5a83e6e6 │ Warning: RPC hizoJc returned null result data   │ -     │ -       │
│          │ (possible server error or parameter mismatch)   │       │         │
└──────────┴─────────────────────────────────────────────────┴───────┴─────────┘


foram executados os comandos:

$ notebooklm use 5a83e6e6
Matched: 5a83e6e6-810... (Guia do Aplicativo Smart Força de Vendas)
08:31:05 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.325s
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ ID       ┃ Title                                           ┃ Owner ┃ Created ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ 5a83e6e6 │ Warning: RPC hizoJc returned null result data   │ -     │ -       │
│          │ (possible server error or parameter mismatch)   │       │         │
└──────────┴─────────────────────────────────────────────────┴───────┴─────────┘



Thinking: The login command opened a browser but was aborted because I didn't press Enter. This is an interactive command that requires user input. Let me try a different approach - maybe we can check for updates or try to understand if this is a known issue with the current version.
Let me check if there's a newer version available or if we can find more information about this RPC error.
# Check for notebooklm updates
$ pip search notebooklm-py 2>/dev/null || pip index versions notebooklm-py 2>/dev/null || echo "Cannot search, checking current version"
notebooklm-py (0.3.4)
Available versions: 0.3.4, 0.3.3, 0.3.2, 0.3.1, 0.3.0, 0.2.1, 0.2.0, 0.1.4, 0.1.3, 0.1.2, 0.1.1
  INSTALLED: 0.3.4
  LATEST:    0.3.4
Thinking: We have the latest version. The issue seems to be with the NotebookLM API itself. The GET_NOTEBOOK RPC call is failing with "returned null result data" which suggests Google may have changed their API or there's a server-side issue.
Let me try a few more troubleshooting steps:
1. Check if we can use the --json flag to get more information
2. Try to see if there's a way to bypass the GET_NOTEBOOK call
3. Maybe try to clear any cached data
Let me try with the --json flag to see if we get more detailed error information.
# Try with JSON output for more details
$ notebooklm source list -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 --json 2>&1
09:16:29 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.357s
{
  "error": true,
  "code": "ERROR",
  "message": "RPC hizoJc returned null result data (possible server error or parameter mismatch)"
}
Thinking: The JSON output doesn't give us more information. The error is consistent. Let me try to see if there's a way to work around this by checking if the source add command works (which might use a different RPC call), or if we can try a different approach.
Actually, let me check if this is a known issue by looking at the GitHub repository or trying to understand what GET_NOTEBOOK does. The error suggests that the RPC call hizoJc (likely a randomly generated ID) returned null data.
Let me try one more thing - maybe we can try to reinstall the package from the latest release tag as suggested in the skill documentation.
# Get latest release tag for reinstall
$ LATEST_TAG=$(curl -s https://api.github.com/repos/teng-lin/notebooklm-py/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && echo "Latest tag: $LATEST_TAG"
Latest tag: v0.3.4
Thinking: We're already on the latest version. The issue appears to be with Google's API or server-side. Let me try one more approach - maybe we can check if there's a way to bypass the GET_NOTEBOOK call by using a different command or approach.
Actually, looking at the error more carefully: "RPC hizoJc returned null result data" - the "hizoJc" is likely a randomly generated RPC ID. The fact that it returns null data suggests that Google's server is not responding with the expected data format for the GET_NOTEBOOK call.
This could be:
1. A temporary server issue on Google's side
2. An API change that the library hasn't been updated for
3. An authentication issue (though auth check passes)
Let me try one more thing - maybe we can try to use a different command that doesn't require GET_NOTEBOOK, or try to see if we can get the notebook information through a different method.
Actually, let me check if the notebooklm use command sets the context even if it fails. We saw earlier that it shows a warning but might still set the notebook ID.
# Try setting context and check status
$ notebooklm use 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>/dev/null; notebooklm status
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ ID                           ┃ Title                       ┃ Owner ┃ Created ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ 101c21c5-2cd9-43c0-9dac-464… │ Warning: RPC hizoJc         │ -     │ -       │
│                              │ returned null result data   │       │         │
│                              │ (possible server error or   │       │         │
│                              │ parameter mismatch)         │       │         │
└──────────────────────────────┴─────────────────────────────┴───────┴─────────┘
                    Current Context                    
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
…
Click to expand
Thinking: Interesting! The notebooklm use command actually sets the notebook ID in the context even though it fails with an error. The notebook ID is now set to 101c21c5-2cd9-43c0-9dac-464e14f21aa0 which is the "Reforma tributária - Débora e Jacqueline" notebook. However, the title and created date show "-" because the GET_NOTEBOOK call failed.
Now, the question is: can we use other commands that rely on this notebook context? Let me try the source list command without the -n flag since the context is already set.
# Try source list with set context
$ notebooklm source list
09:16:41 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.322s
Error: RPC hizoJc returned null result data (possible server error or parameter 
mismatch)
Thinking: The source list command still fails because it calls GET_NOTEBOOK internally to validate the notebook exists and get its details. This is a fundamental issue with the library.
Let me think about alternatives:
1. Use the NotebookLM web interface - This is the most reliable option
2. Try a different library or approach - There might be other ways to access NotebookLM
3. Wait for an update - The library might need to be updated for Google's API changes
For now, the user asked to list sources in a specific notebook. Since the CLI is not working due to the GET_NOTEBOOK RPC error, I should inform them of this issue and suggest using the web interface.
Let me provide a clear summary of the issue and the workaround.
Issue Identified: NotebookLM CLI has a persistent RPC error (GET_NOTEBOOK failed) affecting all operations that need notebook details.
Current Status:
- ✅ notebooklm list works (lists notebooks)
- ❌ notebooklm source list fails (needs GET_NOTEBOOK)
- ❌ notebooklm ask fails (needs GET_NOTEBOOK)
- ❌ notebooklm use sets ID but fails to fetch details
Workaround:
Use the NotebookLM web interface:
1. Go to https://notebooklm.google.com
2. Open "Reforma tributária - Débora e Jacqueline"
3. View sources directly in the UI
Root Cause:
Google may have updated their API, and the notebooklm-py library (v0.3.4) needs an update to handle the new response format.
Would you like me to:
1. Check for alternative ways to access your notebook data?
2. Help you use the web interface?
3. Try a different approach to get the information you need?
▣  Build · mimo-v2-omni-free · 40.0s
<!-- notebooklm-py v0.3.4 -->
# NotebookLM Automation
Complete programmatic access to Google NotebookLM—including capabilities not exposed in the web UI. Create notebooks, add sources (URLs, YouTube, PDFs, audio, video, images), chat with content, generate all artifact types, and download results in multiple formats.
## Installation
**From PyPI (Recommended):**
```bash
pip install notebooklm-py
```
**From GitHub (use latest release tag, NOT main branch):**
```bash
# Get the latest release tag (using curl)
LATEST_TAG=$(curl -s https://api.github.com/repos/teng-lin/notebooklm-py/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
pip install "git+https://github.com/teng-lin/notebooklm-py@${LATEST_TAG}"
```
⚠️ **DO NOT install from main branch** (`pip install git+https://github.com/teng-lin/notebooklm-py`). The main branch may contain unreleased/unstable changes. Always use PyPI or a specific release tag, unless you are testing unreleased features.
After installation, install the Claude Code skill:
```bash
notebooklm skill install
```
## Prerequisites
**IMPORTANT:** Before using any command, you MUST authenticate:
```bash
notebooklm login          # Opens browser for Google OAuth
notebooklm list           # Verify authentication works
```
If commands fail with authentication errors, re-run `notebooklm login`.
### CI/CD, Multiple Accounts, and Parallel Agents
For automated environments, multiple accounts, or parallel agent workflows:
| Variable | Purpose |
|----------|---------|
| `NOTEBOOKLM_HOME` | Custom config directory (default: `~/.notebooklm`) |
| `NOTEBOOKLM_AUTH_JSON` | Inline auth JSON - no file writes needed |
**CI/CD setup:** Set `NOTEBOOKLM_AUTH_JSON` from a secret containing your `storage_state.json` contents.
**Multiple accounts:** Use different `NOTEBOOKLM_HOME` directories per account.
**Parallel agents:** The CLI stores notebook context in a shared file (`~/.notebooklm/context.json`). Multiple concurrent agents using `notebooklm use` can overwrite each other's context.
**Solutions for parallel workflows:**
1. **Always use explicit notebook ID** (recommended): Pass `-n <notebook_id>` (for `wait`/`download` commands) or `--notebook <notebook_id>` (for others) instead of relying on `use`
2. **Per-agent isolation:** Set unique `NOTEBOOKLM_HOME` per agent: `export NOTEBOOKLM_HOME=/tmp/agent-$ID`
3. **Use full UUIDs:** Avoid partial IDs in automation (they can become ambiguous)
## Agent Setup Verification
Before starting workflows, verify the CLI is ready:
1. `notebooklm status` → Should show "Authenticated as: email@..."
2. `notebooklm list --json` → Should return valid JSON (even if empty notebooks list)
3. If either fails → Run `notebooklm login`
## When This Skill Activates
**Explicit:** User says "/notebooklm", "use notebooklm", or mentions the tool by name
**Intent detection:** Recognize requests like:
- "Create a podcast about [topic]"
- "Summarize these URLs/documents"
- "Generate a quiz from my research"
- "Turn this into an audio overview"
- "Create flashcards for studying"
- "Generate a video explainer"
- "Make an infographic"
- "Create a mind map of the concepts"
- "Download the quiz as markdown"
- "Add these sources to NotebookLM"
## Autonomy Rules
**Run automatically (no confirmation):**
- `notebooklm status` - check context
- `notebooklm auth check` - diagnose auth issues
- `notebooklm list` - list notebooks
- `notebooklm source list` - list sources
- `notebooklm artifact list` - list artifacts
- `notebooklm language list` - list supported languages
- `notebooklm language get` - get current language
- `notebooklm language set` - set language (global setting)
- `notebooklm artifact wait` - wait for artifact completion (in subagent context)
- `notebooklm source wait` - wait for source processing (in subagent context)
- `notebooklm research status` - check research status
- `notebooklm research wait` - wait for research (in subagent context)
- `notebooklm use <id>` - set context (⚠️ SINGLE-AGENT ONLY - use `-n` flag in parallel workflows)
- `notebooklm create` - create notebook
- `notebooklm ask "..."` - chat queries (without `--save-as-note`)
- `notebooklm history` - display conversation history (read-only)
- `notebooklm source add` - add sources
**Ask before running:**
- `notebooklm delete` - destructive
- `notebooklm generate *` - long-running, may fail
- `notebooklm download *` - writes to filesystem
- `notebooklm artifact wait` - long-running (when in main conversation)
- `notebooklm source wait` - long-running (when in main conversation)
- `notebooklm research wait` - long-running (when in main conversation)
- `notebooklm ask "..." --save-as-note` - writes a note
- `notebooklm history --save` - writes a note
## Quick Reference
| Task | Command |
|------|---------|
| Authenticate | `notebooklm login` |
| Diagnose auth issues | `notebooklm auth check` |
| Diagnose auth (full) | `notebooklm auth check --test` |
| List notebooks | `notebooklm list` |
| Create notebook | `notebooklm create "Title"` |
| Set context | `notebooklm use <notebook_id>` |
| Show context | `notebooklm status` |
| Add URL source | `notebooklm source add "https://..."` |
| Add file | `notebooklm source add ./file.pdf` |
| Add YouTube | `notebooklm source add "https://youtube.com/..."` |
| List sources | `notebooklm source list` |
| Delete source by ID | `notebooklm source delete <source_id>` |
| Delete source by exact title | `notebooklm source delete-by-title "Exact Title"` |
| Wait for source processing | `notebooklm source wait <source_id>` |
| Web research (fast) | `notebooklm source add-research "query"` |
| Web research (deep) | `notebooklm source add-research "query" --mode deep --no-wait` |
| Check research status | `notebooklm research status` |
| Wait for research | `notebooklm research wait --import-all` |
| Chat | `notebooklm ask "question"` |
| Chat (specific sources) | `notebooklm ask "question" -s src_id1 -s src_id2` |
| Chat (with references) | `notebooklm ask "question" --json` |
| Chat (save answer as note) | `notebooklm ask "question" --save-as-note` |
| Chat (save with title) | `notebooklm ask "question" --save-as-note --note-title "Title"` |
| Show conversation history | `notebooklm history` |
| Save all history as note | `notebooklm history --save` |
| Continue specific conversation | `notebooklm ask "question" -c <conversation_id>` |
| Save history with title | `notebooklm history --save --note-title "My Research"` |
| Get source fulltext | `notebooklm source fulltext <source_id>` |
| Get source guide | `notebooklm source guide <source_id>` |
| Generate podcast | `notebooklm generate audio "instructions"` |
| Generate podcast (JSON) | `notebooklm generate audio --json` |
| Generate podcast (specific sources) | `notebooklm generate audio -s src_id1 -s src_id2` |
| Generate video | `notebooklm generate video "instructions"` |
| Generate report | `notebooklm generate report --format briefing-doc` |
| Generate report (append instructions) | `notebooklm generate report --format study-guide --append "Target audience: beginners"` |
| Generate quiz | `notebooklm generate quiz` |
| Revise a slide | `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0` |
| Check artifact status | `notebooklm artifact list` |
| Wait for completion | `notebooklm artifact wait <artifact_id>` |
| Download audio | `notebooklm download audio ./output.mp3` |
| Download video | `notebooklm download video ./output.mp4` |
| Download slide deck (PDF) | `notebooklm download slide-deck ./slides.pdf` |
| Download slide deck (PPTX) | `notebooklm download slide-deck ./slides.pptx --format pptx` |
| Download report | `notebooklm download report ./report.md` |
| Download mind map | `notebooklm download mind-map ./map.json` |
| Download data table | `notebooklm download data-table ./data.csv` |
| Download quiz | `notebooklm download quiz quiz.json` |
| Download quiz (markdown) | `notebooklm download quiz --format markdown quiz.md` |
| Download flashcards | `notebooklm download flashcards cards.json` |
| Download flashcards (markdown) | `notebooklm download flashcards --format markdown cards.md` |
| Delete notebook | `notebooklm notebook delete <id>` |
| List languages | `notebooklm language list` |
| Get language | `notebooklm language get` |
| Set language | `notebooklm language set zh_Hans` |
**Parallel safety:** Use explicit notebook IDs in parallel workflows. Commands supporting `-n` shorthand: `artifact wait`, `source wait`, `research wait/status`, `download *`. Download commands also support `-a/--artifact`. Other commands use `--notebook`. For chat, use `-c <conversation_id>` to target a specific conversation.
**Partial IDs:** Use first 6+ characters of UUIDs. Must be unique prefix (fails if ambiguous). Works for ID-based commands such as `use`, `source delete`, and `wait`. For exact source-title deletion, use `source delete-by-title "Title"`. For automation, prefer full UUIDs to avoid ambiguity.
## Command Output Formats
Commands with `--json` return structured data for parsing:
**Create notebook:**
```
$ notebooklm create "Research" --json
{"id": "abc123de-...", "title": "Research"}
```
**Add source:**
```
$ notebooklm source add "https://example.com" --json
{"source_id": "def456...", "title": "Example", "status": "processing"}
```
**Generate artifact:**
```
$ notebooklm generate audio "Focus on key points" --json
{"task_id": "xyz789...", "status": "pending"}
```
**Chat with references:**
```
$ notebooklm ask "What is X?" --json
{"answer": "X is... [1] [2]", "conversation_id": "...", "turn_number": 1, "is_follow_up": false, "references": [{"source_id": "abc123...", "citation_number": 1, "cited_text": "Relevant passage from source..."}, {"source_id": "def456...", "citation_number": 2, "cited_text": "Another passage..."}]}
```
**Source fulltext (get indexed content):**
```
$ notebooklm source fulltext <source_id> --json
{"source_id": "...", "title": "...", "char_count": 12345, "content": "Full indexed text..."}
```
**Understanding citations:** The `cited_text` in references is often a snippet or section header, not the full quoted passage. The `start_char`/`end_char` positions reference NotebookLM's internal chunked index, not the raw fulltext. Use `SourceFulltext.find_citation_context()` to locate citations:
```python
fulltext = await client.sources.get_fulltext(notebook_id, ref.source_id)
matches = fulltext.find_citation_context(ref.cited_text)  # Returns list[(context, position)]
if matches:
    context, pos = matches[0]  # First match; check len(matches) > 1 for duplicates
```
**Extract IDs:** Parse the `id`, `source_id`, or `task_id` field from JSON output.
## Generation Types
All generate commands support:
- `-s, --source` to use specific source(s) instead of all sources
- `--language` to set output language (defaults to configured language or 'en')
- `--json` for machine-readable output (returns `task_id` and `status`)
- `--retry N` to automatically retry on rate limits with exponential backoff
| Type | Command | Options | Download |
|------|---------|---------|----------|
| Podcast | `generate audio` | `--format [deep-dive\|brief\|critique\|debate]`, `--length [short\|default\|long]` | .mp3 |
| Video | `generate video` | `--format [explainer\|brief]`, `--style [auto\|classic\|whiteboard\|kawaii\|anime\|watercolor\|retro-print\|heritage\|paper-craft]` | .mp4 |
| Slide Deck | `generate slide-deck` | `--format [detailed\|presenter]`, `--length [default\|short]` | .pdf / .pptx |
| Slide Revision | `generate revise-slide "prompt" --artifact <id> --slide N` | `--wait`, `--notebook` | *(re-downloads parent deck)* |
| Infographic | `generate infographic` | `--orientation [landscape\|portrait\|square]`, `--detail [concise\|standard\|detailed]`, `--style [auto\|sketch-note\|professional\|bento-grid\|editorial\|instructional\|bricks\|clay\|anime\|kawaii\|scientific]` | .png |
| Report | `generate report` | `--format [briefing-doc\|study-guide\|blog-post\|custom]`, `--append "extra instructions"` | .md |
| Mind Map | `generate mind-map` | *(sync, instant)* | .json |
| Data Table | `generate data-table` | description required | .csv |
| Quiz | `generate quiz` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |
| Flashcards | `generate flashcards` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |
## Features Beyond the Web UI
These capabilities are available via CLI but not in NotebookLM's web interface:
| Feature | Command | Description |
|---------|---------|-------------|
| **Batch downloads** | `download <type> --all` | Download all artifacts of a type at once |
| **Quiz/Flashcard export** | `download quiz --format json` | Export as JSON, Markdown, or HTML (web UI only shows interactive view) |
| **Mind map extraction** | `download mind-map` | Export hierarchical JSON for visualization tools |
| **Data table export** | `download data-table` | Download structured tables as CSV |
| **Slide deck as PPTX** | `download slide-deck --format pptx` | Download slide deck as editable .pptx (web UI only offers PDF) |
| **Slide revision** | `generate revise-slide "prompt" --artifact <id> --slide N` | Modify individual slides with a natural-language prompt |
| **Report template append** | `generate report --format study-guide --append "..."` | Append custom instructions to built-in format templates without losing the format type |
| **Source fulltext** | `source fulltext <id>` | Retrieve the indexed text content of any source |
| **Save chat to note** | `ask "..." --save-as-note` / `history --save` | Save Q&A answers or conversation history as notebook notes |
| **Programmatic sharing** | `share` commands | Manage sharing permissions without the UI |
## Common Workflows
### Research to Podcast (Interactive)
**Time:** 5-10 minutes total
1. `notebooklm create "Research: [topic]"` — *if fails: check auth with `notebooklm login`*
2. `notebooklm source add` for each URL/document — *if one fails: log warning, continue with others*
3. Wait for sources: `notebooklm source list --json` until all status=READY — *required before generation*
4. `notebooklm generate audio "Focus on [specific angle]"` (confirm when asked) — *if rate limited: wait 5 min, retry once*
5. Note the artifact ID returned
6. Check `notebooklm artifact list` later for status
7. `notebooklm download audio ./podcast.mp3` when complete (confirm when asked)
### Research to Podcast (Automated with Subagent)
**Time:** 5-10 minutes, but continues in background
When user wants full automation (generate and download when ready):
1. Create notebook and add sources as usual
2. Wait for sources to be ready (use `source wait` or check `source list --json`)
3. Run `notebooklm generate audio "..." --json` → parse `artifact_id` from output
4. **Spawn a background agent** using Task tool:
   ```
   Task(
     prompt="Wait for artifact {artifact_id} in notebook {notebook_id} to complete, then download.
             Use: notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 600
             Then: notebooklm download audio ./podcast.mp3 -a {artifact_id} -n {notebook_id}",
     subagent_type="general-purpose"
   )
   ```
5. Main conversation continues while agent waits
**Error handling in subagent:**
- If `artifact wait` returns exit code 2 (timeout): Report timeout, suggest checking `artifact list`
- If download fails: Check if artifact status is COMPLETED first
**Benefits:** Non-blocking, user can do other work, automatic download on completion
### Document Analysis
**Time:** 1-2 minutes
1. `notebooklm create "Analysis: [project]"`
2. `notebooklm source add ./doc.pdf` (or URLs)
3. `notebooklm ask "Summarize the key points"`
4. `notebooklm ask "What are the main arguments?"`
5. Continue chatting as needed
### Bulk Import
**Time:** Varies by source count
1. `notebooklm create "Collection: [name]"`
2. Add multiple sources:
   ```bash
   notebooklm source add "https://url1.com"
   notebooklm source add "https://url2.com"
   notebooklm source add ./local-file.pdf
   ```
3. `notebooklm source list` to verify
**Source limits:** Varies by plan—Standard: 50, Plus: 100, Pro: 300, Ultra: 600 sources per notebook. See [NotebookLM plans](https://support.google.com/notebooklm/answer/16213268) for details. The CLI does not enforce these limits; they are applied by your NotebookLM account.
**Supported types:** PDFs, YouTube URLs, web URLs, Google Docs, text files, Markdown, Word docs, audio files, video files, images
### Bulk Import with Source Waiting (Subagent Pattern)
**Time:** Varies by source count
When adding multiple sources and needing to wait for processing before chat/generation:
1. Add sources with `--json` to capture IDs:
   ```bash
   notebooklm source add "https://url1.com" --json  # → {"source_id": "abc..."}
   notebooklm source add "https://url2.com" --json  # → {"source_id": "def..."}
   ```
2. **Spawn a background agent** to wait for all sources:
   ```
   Task(
     prompt="Wait for sources {source_ids} in notebook {notebook_id} to be ready.
             For each: notebooklm source wait {id} -n {notebook_id} --timeout 120
             Report when all ready or if any fail.",
     subagent_type="general-purpose"
   )
   ```
3. Main conversation continues while agent waits
4. Once sources are ready, proceed with chat or generation
**Why wait for sources?** Sources must be indexed before chat or generation. Takes 10-60 seconds per source.
### Deep Web Research (Subagent Pattern)
**Time:** 2-5 minutes, runs in background
Deep research finds and analyzes web sources on a topic:
1. Create notebook: `notebooklm create "Research: [topic]"`
2. Start deep research (non-blocking):
   ```bash
   notebooklm source add-research "topic query" --mode deep --no-wait
   ```
3. **Spawn a background agent** to wait and import:
   ```
   Task(
     prompt="Wait for research in notebook {notebook_id} to complete and import sources.
             Use: notebooklm research wait -n {notebook_id} --import-all --timeout 300
             Report how many sources were imported.",
     subagent_type="general-purpose"
   )
   ```
4. Main conversation continues while agent waits
5. When agent completes, sources are imported automatically
**Alternative (blocking):** For simple cases, omit `--no-wait`:
```bash
notebooklm source add-research "topic" --mode deep --import-all
# Blocks for up to 5 minutes
```
**When to use each mode:**
- `--mode fast`: Specific topic, quick overview needed (5-10 sources, seconds)
- `--mode deep`: Broad topic, comprehensive analysis needed (20+ sources, 2-5 min)
**Research sources:**
- `--from web`: Search the web (default)
- `--from drive`: Search Google Drive
## Output Style
**Progress updates:** Brief status for each step
- "Creating notebook 'Research: AI'..."
- "Adding source: https://example.com..."
- "Starting audio generation... (task ID: abc123)"
**Fire-and-forget for long operations:**
- Start generation, return artifact ID immediately
- Do NOT poll or wait in main conversation - generation takes 5-45 minutes (see timing table)
- User checks status manually, OR use subagent with `artifact wait`
**JSON output:** Use `--json` flag for machine-readable output:
```bash
notebooklm list --json
notebooklm auth check --json
notebooklm source list --json
notebooklm artifact list --json
```
**JSON schemas (key fields):**
`notebooklm list --json`:
```json
{"notebooks": [{"id": "...", "title": "...", "created_at": "..."}]}
```
`notebooklm auth check --json`:
```json
{"checks": {"storage_exists": true, "json_valid": true, "cookies_present": true, "sid_cookie": true, "token_fetch": true}, "details": {"storage_path": "...", "auth_source": "file", "cookies_found": ["SID", "HSID", "..."], "cookie_domains": [".google.com"]}}
```
`notebooklm source list --json`:
```json
{"sources": [{"id": "...", "title": "...", "status": "ready|processing|error"}]}
```
`notebooklm artifact list --json`:
```json
{"artifacts": [{"id": "...", "title": "...", "type": "Audio Overview", "status": "in_progress|pending|completed|unknown"}]}
```
**Status values:**
- Sources: `processing` → `ready` (or `error`)
- Artifacts: `pending` or `in_progress` → `completed` (or `unknown`)
## Error Handling
**On failure, offer the user a choice:**
1. Retry the operation
2. Skip and continue with something else
3. Investigate the error
**Error decision tree:**
| Error | Cause | Action |
|-------|-------|--------|
| Auth/cookie error | Session expired | Run `notebooklm auth check` then `notebooklm login` |
| "No notebook context" | Context not set | Use `-n <id>` or `--notebook <id>` flag (parallel), or `notebooklm use <id>` (single-agent) |
| "No result found for RPC ID" | Rate limiting | Wait 5-10 min, retry |
| `GENERATION_FAILED` | Google rate limit | Wait and retry later |
| Download fails | Generation incomplete | Check `artifact list` for status |
| Invalid notebook/source ID | Wrong ID | Run `notebooklm list` to verify |
| RPC protocol error | Google changed APIs | May need CLI update |
## Exit Codes
All commands use consistent exit codes:
| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 1 | Error (not found, processing failed) | Check stderr, see Error Handling |
| 2 | Timeout (wait commands only) | Extend timeout or check status manually |
**Examples:**
- `source wait` returns 1 if source not found or processing failed
- `artifact wait` returns 2 if timeout reached before completion
- `generate` returns 1 if rate limited (check stderr for details)
## Known Limitations
**Rate limiting:** Audio, video, quiz, flashcards, infographic, and slide deck generation may fail due to Google's rate limits. This is an API limitation, not a bug.
**Reliable operations:** These always work:
- Notebooks (list, create, delete, rename)
- Sources (add, list, delete)
- Chat/queries
- Mind-map, study-guide, report, data-table generation
**Unreliable operations:** These may fail with rate limiting:
- Audio (podcast) generation
- Video generation
- Quiz and flashcard generation
- Infographic and slide deck generation
**Workaround:** If generation fails:
1. Check status: `notebooklm artifact list`
2. Retry after 5-10 minutes
3. Use the NotebookLM web UI as fallback
**Processing times vary significantly.** Use the subagent pattern for long operations:
| Operation | Typical time | Suggested timeout |
|-----------|--------------|-------------------|
| Source processing | 30s - 10 min | 600s |
| Research (fast) | 30s - 2 min | 180s |
| Research (deep) | 15 - 30+ min | 1800s |
| Notes | instant | n/a |
| Mind-map | instant (sync) | n/a |
| Quiz, flashcards | 5 - 15 min | 900s |
| Report, data-table | 5 - 15 min | 900s |
| Audio generation | 10 - 20 min | 1200s |
| Video generation | 15 - 45 min | 2700s |
**Polling intervals:** When checking status manually, poll every 15-30 seconds to avoid excessive API calls.
## Language Configuration
Language setting controls the output language for generated artifacts (audio, video, etc.).
**Important:** Language is a **GLOBAL** setting that affects all notebooks in your account.
```bash
# List all 80+ supported languages with native names
notebooklm language list
# Show current language setting
notebooklm language get
# Set language for artifact generation
notebooklm language set zh_Hans  # Simplified Chinese
notebooklm language set ja       # Japanese
notebooklm language set en       # English (default)
```
**Common language codes:**
| Code | Language |
|------|----------|
| `en` | English |
| `zh_Hans` | 中文（简体） - Simplified Chinese |
| `zh_Hant` | 中文（繁體） - Traditional Chinese |
| `ja` | 日本語 - Japanese |
| `ko` | 한국어 - Korean |
| `es` | Español - Spanish |
| `fr` | Français - French |
| `de` | Deutsch - German |
| `pt_BR` | Português (Brasil) |
**Override per command:** Use `--language` flag on generate commands:
```bash
notebooklm generate audio --language ja   # Japanese podcast
notebooklm generate video --language zh_Hans  # Chinese video
```
**Offline mode:** Use `--local` flag to skip server sync:
```bash
notebooklm language set zh_Hans --local  # Save locally only
notebooklm language get --local  # Read local config only
```
## Troubleshooting
```bash
notebooklm --help              # Main commands
notebooklm auth check          # Diagnose auth issues
notebooklm auth check --test   # Full auth validation with network test
notebooklm notebook --help     # Notebook management
notebooklm source --help       # Source management
notebooklm research --help     # Research status/wait
notebooklm generate --help     # Content generation
notebooklm artifact --help     # Artifact management
notebooklm download --help     # Download content
notebooklm language --help     # Language settings
```
**Diagnose auth:** `notebooklm auth check` - shows cookie domains, storage path, validation status
**Re-authenticate:** `notebooklm login`
**Check version:** `notebooklm --version`
**Update skill:** `notebooklm skill install`
Thinking: The user pasted the entire notebooklm skill documentation. They might be trying to help me understand how to fix the RPC error or they're just pasting information. Looking at the system reminder, it says my operational mode has changed from plan to build, meaning I can now make file changes and run commands.
The RPC error seems to be a known issue - in the documentation under "Error Handling", it mentions "RPC protocol error" with "Google changed APIs" and "May need CLI update" as the action. This aligns with what we've been experiencing.
Let me try a few more troubleshooting steps:
1. Maybe try reinstalling from the latest GitHub release tag (though we already checked it's v0.3.4)
2. Try to see if there's any debug mode or verbose output
3. Check if there are any environment variables that might help
Actually, looking at the error more carefully: "RPC hizoJc returned null result data" - the RPC ID is randomly generated each time. The fact that it returns null data suggests the server is not returning the expected data format.
Let me try to see if there's a way to get more debug information or if there's a known workaround. I could also try to see if the issue is specific to the Google API endpoint by checking if we can make a direct API call.
But first, let me try one more thing - maybe we need to clear some cache or configuration. Let me check the notebooklm home directory.
# Check notebooklm config directory
$ ls -la ~/.notebooklm/ 2>/dev/null || echo "Directory not found"
total 48
drwx------@   6 arpasistemas  staff    192 Mar 19 17:33 .
drwxr-x---+ 126 arpasistemas  staff   4032 Mar 20 08:38 ..
drwx------@  18 arpasistemas  staff    576 Mar 20 09:16 browser_profile
-rw-r--r--@   1 arpasistemas  staff     25 Mar 20 08:28 config.json
-rw-r--r--@   1 arpasistemas  staff     59 Mar 20 09:16 context.json
-rw-------@   1 arpasistemas  staff  13679 Mar 20 08:28 storage_state.json
Thinking: I can see the notebooklm configuration directory. Let me check what's in the context.json file to see what notebook is currently set.
# Check current notebook context
$ cat ~/.notebooklm/context.json
{
  "notebook_id": "101c21c5-2cd9-43c0-9dac-464e14f21aa0"
}
Thinking: Good, the context is set to the correct notebook (Reforma tributária - Débora e Jacqueline). Now let me check if there's a way to bypass the GET_NOTEBOOK RPC call. Looking at the error, it seems like the CLI is trying to fetch notebook details before performing operations, and this call is failing.
Let me try a different approach - maybe we can use the Python library directly or see if there's a way to disable the notebook validation. First, let me check if there are any command-line options that might help with debugging.
# Check notebooklm help for debug options
$ notebooklm --help
Usage: notebooklm [OPTIONS] COMMAND [ARGS]...
  NotebookLM CLI.
  Quick start:
    notebooklm login              # Authenticate first
    notebooklm list               # List your notebooks
    notebooklm create "My Notes"  # Create a notebook
    notebooklm ask "Hi"           # Ask the current notebook a question
…
Click to expand
Thinking: I see there's a -v or --verbose option for debugging. Let me try using that to get more information about the error.
# Try with verbose output
$ notebooklm source list -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 -vv 2>&1
Usage: notebooklm source list [OPTIONS]
Try 'notebooklm source list --help' for help.
Error: No such option: -v
Thinking: The verbose flag needs to be before the command. Let me try it correctly.
# Try with verbose flag before command
$ notebooklm -vv source list -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1 | head -50
09:18:05 DEBUG [notebooklm.cli.helpers] CLI command starting: source_list
09:18:05 DEBUG [notebooklm.auth] Cookie HSID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie SSID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie APISID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie SAPISID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie __Secure-1PAPISID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie __Secure-3PAPISID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie SID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie __Secure-1PSID: ignoring duplicate from .google.com.br (keeping .google.com)
09:18:05 DEBUG [notebooklm.auth] Cookie __Secure-3PSID: ignoring duplicate from .google.com.br (keeping .google.com)
…
Click to expand
Thinking: Excellent! Now I have detailed debug information. The issue is clear:
1. The RPC method is called hizoJc (this is likely a dynamically generated method name from Google's API)
2. The request is built (199 bytes) and sent
3. The response is received (140 bytes)
4. The response contains the expected RPC ID hizoJc
5. However, the result data is null
This suggests that Google's API is returning a valid response but with null data for the GET_NOTEBOOK method. This could be due to:
1. API version mismatch: The CLI might be using an older API version that Google has changed
2. Parameter mismatch: The parameters sent might not be correct for the current API
3. Authentication issue: Even though auth passes, there might be missing required cookies or tokens
Looking at the debug output, I can see the method name is hizoJc. This appears to be a randomly generated method name that changes with each request (based on the different RPC IDs we've seen: hizoJc, etc.). This suggests Google is using some form of obfuscation or dynamic method naming.
Let me try to see if we can get more information about the actual request being sent. I notice the request body is 199 bytes. Let me see if I can capture more details.
Actually, looking at this more carefully, I think the issue might be that Google has changed their API and the notebooklm-py library needs to be updated. The fact that notebooklm list works but GET_NOTEBOOK fails suggests that different endpoints are using different API methods.
Let me try one more thing - maybe we can try using the metadata command which might use a different API call.
# Try metadata export
$ notebooklm metadata export -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
Usage: notebooklm metadata [OPTIONS]
Try 'notebooklm metadata --help' for help.
Error: Got unexpected extra argument (export)
Thinking: Let me check the metadata command help.
# Check metadata command help
$ notebooklm metadata --help
Usage: notebooklm metadata [OPTIONS]
  Export notebook metadata with sources list.
  Outputs notebook details (id, title, created_at, is_owner) along with a
  simplified list of sources (type, title, url).
  By default, outputs in human-readable format. Use --json for machine
  parsing.
…
Click to expand
Thinking: Let me try the metadata command - it might use a different API call that works.
# Try metadata command
$ notebooklm metadata -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
09:18:15 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.315s
09:18:15 ERROR [notebooklm._core] RPC GET_NOTEBOOK failed after 0.316s: 
Error: RPC hizoJc returned null result data (possible server error or parameter 
mismatch)
Thinking: The metadata command also fails with the same GET_NOTEBOOK RPC error. This confirms that all operations that need notebook details are affected.
Looking at the debug output again, I notice:
1. The RPC method name hizoJc appears to be randomly generated
2. The request is being sent and a response is received
3. The response contains the expected RPC ID but with null data
This is a known issue mentioned in the skill documentation under "Error Handling" - "RPC protocol error" with "Google changed APIs" as the cause.
Let me try one more approach - maybe we can try to reinstall from the latest GitHub release, even though the version is the same. Sometimes there might be minor fixes that aren't reflected in the version number.
But first, let me check if there's a way to see what the actual API call looks like. I could try to see if there's a way to intercept the HTTP requests.
Actually, looking at the debug output more carefully, I see that the request is 199 bytes and the response is 140 bytes. This is quite small for a notebook details response. A typical notebook response should include title, created date, owner info, etc. The fact that it's only 140 bytes suggests the response is mostly headers with empty data.
Let me try to reinstall the package from the latest GitHub release, even though it's the same version, to make sure we have the absolute latest code.
# Reinstall from latest GitHub release
$ LATEST_TAG=$(curl -s https://api.github.com/repos/teng-lin/notebooklm-py/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && pip install --force-reinstall "git+https://github.com/teng-lin/notebooklm-py@${LATEST_TAG}" 2>&1 | tail -20
Found existing installation: anyio 4.12.1
    Uninstalling anyio-4.12.1:
      Successfully uninstalled anyio-4.12.1
  Attempting uninstall: rich
    Found existing installation: rich 14.3.3
    Uninstalling rich-14.3.3:
      Successfully uninstalled rich-14.3.3
  Attempting uninstall: httpx
    Found existing installation: httpx 0.28.1
    Uninstalling httpx-0.28.1:
…
Click to expand
Thinking: The package was reinstalled, but it's still version 0.3.4. Let me try the source list command again to see if the issue persists.
# Test after reinstall
$ notebooklm source list -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
Sources in 101c21c5-2cd9-43c0-9dac-464e14f21aa0                 
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ ID              ┃ Title          ┃ Type           ┃ Created         ┃ Status ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ 22b37de9-f56a-… │ Ebook          │ 📄 PDF         │ 2025-07-16      │ ready  │
│                 │ AFRAC.pdf      │                │ 16:20           │        │
│ dc3e6e09-6cbc-… │ Ebook SAC      │ 📄 PDF         │ 2025-07-16      │ ready  │
│                 │ Fiscal.pdf     │                │ 16:18           │        │
│ 482e6a9b-655d-… │ Emenda         │ 📄 PDF         │ 2025-07-16      │ ready  │
│                 │ Constitucional │                │ 15:21           │        │
…
Click to expand
Thinking: Excellent! The reinstall worked! The notebooklm source list command now works and shows the sources in the notebook "Reforma tributária - Débora e Jacqueline". 
Now let me try the original question that the user wanted to ask about 2027 tax reform.
The reinstall fixed the RPC error. Now I can access your notebook. Let me ask about 2027 tax reform:
# Ask about 2027 tax reform
$ notebooklm ask "O que temos para 2027 na reforma tributária?" -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
Continuing conversation 3813fdf8...
Answer:
O ano de 2027 será um marco fundamental na Reforma Tributária, caracterizado 
pelo início da transição efetiva do modelo atual para o novo sistema de 
tributação sobre o consumo [1, 2]. 
As principais mudanças e implementações previstas para 2027 são:
*   **Fim do PIS e da COFINS e início da CBS:** Ocorrerá a extinção total do PIS
e da COFINS, que serão substituídos pela cobrança efetiva da Contribuição sobre 
…
Click to expand
Thinking: The question was answered successfully! The notebook contained detailed information about the 2027 tax reform changes. Let me provide a summary to the user.
Resumo para 2027 na Reforma Tributária:
O ano de 2027 será um marco fundamental com as seguintes mudanças:
1. Fim do PIS/COFINS → Início da CBS
2. Início do Imposto Seletivo (IS) - "imposto do pecado"
3. Redução do IPI a zero (exceto Zona Franca de Manaus)
4. Cobrança do IBS com alíquota de transição de 0,1%
5. Split Payment facultativo para operações B2B
6. Cashback da CBS para famílias de baixa renda
7. Compras Governamentais - arrecadação do IBS vai para o ente contratante
8. Simples Nacional com tabelas transitórias
Todas as fontes estão disponíveis no notebook (PDFs, vídeos, textos).
▣  Build · mimo-v2-omni-free · 1m 21s
 notebooklm ask "A dilma vai ganhar a eleicao?" -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
Thinking: The user is asking about Dilma winning an election. This is a political question about Brazilian politics. I should note that I'm now in "build" mode (can execute commands), but I should be careful about political predictions. 
The notebook is about tax reform ("Reforma tributária - Débora e Jacqueline"), so it's unlikely to have information about Dilma or elections. The user might be testing or curious. I should run the command to see what the notebook returns.
Let me execute the command.
# Ask about Dilma election
$ notebooklm ask "A dilma vai ganhar a eleicao?" -n 101c21c5-2cd9-43c0-9dac-464e14f21aa0 2>&1
Answer:
As fontes fornecidas e o nosso histórico de conversas não contêm informações 
sobre eleições ou sobre a Dilma. 
Os materiais tratam exclusivamente da **Reforma Tributária no Brasil**, 
abordando detalhadamente a implementação de novos tributos como o Imposto sobre 
Bens e Serviços (IBS), a Contribuição sobre Bens e Serviços (CBS) e o Imposto 
Seletivo (IS), além de explicar o cronograma de transição e os impactos 
tecnológicos nos documentos fiscais eletrônicos.
…
Click to expand
Thinking: The notebook correctly responded that it doesn't have information about Dilma or elections - it's focused on tax reform. This is a good response from the system.
The user might be testing the system or curious. I should provide a concise response noting what the notebook contains.
O notebook confirmou que não tem informações sobre Dilma ou eleições - contém apenas materiais sobre Reforma Tributária.
Fontes disponíveis: PDFs sobre IBS, CBS, IS, legislação, e vídeos sobre implementação da reforma.
▣  Build · mimo-v2-omni-free · 28.9s