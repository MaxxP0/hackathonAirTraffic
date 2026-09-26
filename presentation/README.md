# Air traffic control benchmark presentation

`ATC-Benchmark-Luna-vs-GLM.pptx` contains nine slides comparing the two LLMs, with three embedded GPT-6 Luna videos. Open it in desktop PowerPoint and click a video during Slide Show to play it. The MP4s travel inside the deck. No external video downloads are required.

The score chart and both results tables are native, editable slide objects. Sources and measurement definitions appear in the speaker notes. All runs use seed 7, 1,800 simulated seconds and 120-second command windows. GLM resumed its interrupted episodes with the saved simulator state and conversation.

## Rebuild

The JavaScript builder uses the bundled `@oai/artifact-tool` runtime. Point `presentation/node_modules` at that runtime's Node package directory, then run:

```sh
/path/to/bundled/node presentation/build.mjs --draft
/path/to/bundled/node presentation/build.mjs
```

Optional environment variables are `ATC_PRESENTATION_SKILL` and `ATC_PRESENTATION_PYTHON`, which select the presentation skill directory and bundled Python executable. The defaults match the original workspace.

The full build requires all three canonical GLM episodes in `results/glm-completed-v3` and all three Luna episodes in `results/luna-v3` to have completed. When local run files are absent, it reads their published copies in `docs/evaluations`. It refuses to turn a partial episode into a final score. Move any previous final PPTX before rebuilding. Drafts and validation receipts remain in the ignored `.build` folder.

`embed_videos.py` adds standard OOXML movie relationships and playback actions to the three poster images. The script verifies each embedded file against its original SHA-256. It does not use python-pptx.
