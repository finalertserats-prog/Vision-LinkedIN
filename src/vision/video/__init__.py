"""Video lane — the anime 'Insight Reel' pipeline (BRD §23, Phase 5).

A parallel enrichment lane that turns an approved insight into a 1080x1920
vertical reel: agy anime stills + Ken Burns motion + deterministic burned-in
captions + edge-tts voice-over (+ optional music), assembled with ffmpeg and
uploaded to LinkedIn /rest/videos. Every factual pixel is deterministic; the
generative surface (anime art, TTS voice) only adds text-free motion + warmth.

No API key: agy for stills (existing), edge-tts for voice, imageio-ffmpeg for the
bundled ffmpeg binary. Veo B-roll + voice clone are deferred opt-in phases.
"""
