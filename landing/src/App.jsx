import { useRef, useEffect, useState, useCallback } from 'react'
import { createScene } from './three/scene.js'
import { createParticleOverlay } from './three/particles.js'
import { MarqueeHeading } from './components/MarqueeHeading.jsx'
import { IMessageChat } from './components/IMessageChat.jsx'
import { Reveal } from './components/Reveal.jsx'
import './App.css'

// ── Main App ──
function App() {
  const containerRef = useRef(null)
  const particleRef = useRef(null)
  const [loaded, setLoaded] = useState(false)
  const [sceneError, setSceneError] = useState(false)
  const [navVisible, setNavVisible] = useState(false)
  const [footerVisible, setFooterVisible] = useState(false)

  // Hero 3D scene — pauses when scrolled off-screen
  useEffect(() => {
    if (!containerRef.current) return
    let disposed = false
    let sceneApi
    let heroObs

    createScene(containerRef.current).then((api) => {
      if (disposed) { api.dispose(); return }
      sceneApi = api
      setLoaded(true)

      // Pause hero when off-screen
      heroObs = new IntersectionObserver(([e]) => {
        if (e.isIntersecting) sceneApi?.resume()
        else sceneApi?.pause()
      }, { threshold: 0.05 })
      if (containerRef.current) heroObs.observe(containerRef.current)
    }).catch(err => {
      console.error('Scene init failed:', err)
      setSceneError(true)
    })

    return () => { disposed = true; heroObs?.disconnect(); sceneApi?.dispose() }
  }, [])

  // Particle overlay — 3D kernels drifting along sides during scroll
  useEffect(() => {
    if (!particleRef.current) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const particles = createParticleOverlay(particleRef.current)
    let rafId
    let overlayVisible = false

    function onScroll() {
      if (!particleRef.current) return
      // Fade in particle overlay only after scrolling past the hero
      const progress = Math.min(1, Math.max(0, (window.scrollY - window.innerHeight * 0.6) / (window.innerHeight * 0.3)))
      overlayVisible = progress > 0
      particleRef.current.style.opacity = progress
    }
    particleRef.current.style.opacity = 0
    window.addEventListener('scroll', onScroll, { passive: true })

    function loop(time) {
      rafId = requestAnimationFrame(loop)
      if (overlayVisible) particles.render(time)
    }
    rafId = requestAnimationFrame(loop)

    return () => {
      window.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(rafId)
      particles.dispose()
    }
  }, [])

  // Nav appears after scrolling past hero
  useEffect(() => {
    const onScroll = () => setNavVisible(window.scrollY > window.innerHeight * 0.7)
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  // Footer flicker detection
  const footerObsRef = useRef(null)
  const footerRef = useCallback(node => {
    if (footerObsRef.current) footerObsRef.current.disconnect()
    if (!node) { footerObsRef.current = null; return }
    const obs = new IntersectionObserver(([e]) => setFooterVisible(e.isIntersecting), { threshold: 0.3 })
    obs.observe(node)
    footerObsRef.current = obs
  }, [])

  return (
    <>
      {/* 3D Canvas — hero scene */}
      <div
        id="scene-container"
        ref={containerRef}
        style={{ opacity: loaded ? 1 : 0, transition: 'opacity 0.8s ease-in' }}
      />
      {sceneError && (
        <div className="hero-fallback">
          <div className="hero-fallback-title neon-text">BluePopcorn</div>
        </div>
      )}

      {/* 3D Particle overlay — popcorn kernels drifting along sides */}
      <div id="particle-overlay" ref={particleRef} />

      {/* Nav — appears on scroll */}
      <nav className={`nav ${navVisible ? 'nav-visible' : ''}`}>
        <div className="nav-inner">
          <a href="/" className={`nav-logo neon-text ${navVisible ? 'neon-flicker-on' : ''}`}><img src="/favicon.svg" alt="" width="18" height="18" className="nav-logo-icon" />BluePopcorn</a>
          <div className="nav-links">
            <a href="#setup">Setup</a>
            <a href="#tools">Tools</a>
            <a href="https://github.com/Averyy/bluepopcorn" target="_blank" rel="noopener noreferrer"><svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>GitHub</a>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <header className="hero">
        <h1 className="hero-title sr-only">BluePopcorn</h1>
        <p className="hero-tagline">Seerr MCP server + iMessage chatbot</p>
      </header>

      {/* Content */}
      <main className="content">

        {/* ── Install — ticket stub ── */}
        <section id="setup" className="install-section">
          <Reveal>
            <div className="ticket">
              <div className="ticket-header">ADMIT ONE</div>
              <div className="ticket-body">
                <div className="ticket-title">Get Started</div>
                <div className="install-methods">
                  <div className="install-method">
                    <span className="install-label">Clone</span>
                    <pre className="install-block"><span className="prompt">$</span> <code>git clone https://github.com/Averyy/bluepopcorn.git && cd bluepopcorn && uv sync</code></pre>
                  </div>
                  <div className="install-method">
                    <span className="install-label">MCP Server (stdio)</span>
                    <pre className="install-block"><span className="prompt">$</span> <code>claude mcp add bluepopcorn -- uv run -m bluepopcorn.mcp --stdio</code></pre>
                  </div>
                  <div className="install-method">
                    <span className="install-label">iMessage Bot (macOS)</span>
                    <pre className="install-block"><span className="prompt">$</span> <code>uv run -m bluepopcorn</code></pre>
                  </div>
                </div>
              </div>
              <div className="ticket-tear" />
            </div>
          </Reveal>
        </section>

        {/* ── Intro ── */}
        <section className="intro-section">
          <MarqueeHeading>Your media server, AI-powered.</MarqueeHeading>
          <div className="film-strip">
            <div className="sprockets" />
            <div className="film-content">
              <Reveal>
                <div className="film-frame">
                  <code className="tool-name">MCP Server</code>
                  <p>Add to Claude, Cursor, or any MCP client. Search, recommend, and request movies and TV shows directly from your Seerr instance — your AI handles the API calls.</p>
                </div>
              </Reveal>
              <Reveal delay={0.1}>
                <div className="film-frame">
                  <code className="tool-name">iMessage Bot</code>
                  <p>Run as a daemon on your Mac. Text it what you want to watch and it handles everything — no app needed, just your Messages app.</p>
                </div>
              </Reveal>
            </div>
            <div className="sprockets" />
          </div>
        </section>

        {/* ── MCP Tools — film strip ── */}
        <section id="tools" className="tools-section">
          <MarqueeHeading>MCP server with 5 tools.</MarqueeHeading>
          <div className="film-strip">
            <div className="sprockets" />
            <div className="film-content">
              {[
                { name: 'seerr_search', desc: 'Search movies and TV shows by title. Handles year extraction, fuzzy matching, and typo correction.' },
                { name: 'seerr_recommend', desc: 'Browse by genre, mood, keyword, or similarity. "Something like Severance" — compound queries just work.' },
                { name: 'seerr_request', desc: 'Request a title for download. Deduplicates automatically, fetches all seasons for TV.' },
                { name: 'seerr_details', desc: 'Full info on any title — Rotten Tomatoes, IMDB, trailers, download progress, seasons.' },
                { name: 'seerr_recent', desc: 'What\'s new in the library. Recent additions, pending requests, active downloads.' },
              ].map((tool, i) => (
                <Reveal key={tool.name} delay={i * 0.1}>
                  <div className="film-frame">
                    <code className="tool-name">{tool.name}</code>
                    <p>{tool.desc}</p>
                  </div>
                </Reveal>
              ))}
            </div>
            <div className="sprockets" />
          </div>
        </section>

        {/* ── iMessage ── */}
        <section className="imessage-section">
          <MarqueeHeading>iMessage bot for conversational requests.</MarqueeHeading>
          <div className="imessage-layout">
            <Reveal>
              <div className="imessage-copy">
                <p>Run BluePopcorn as a daemon on your Mac. It monitors iMessage and responds to requests conversationally — with memory of past conversations and personalized suggestions.</p>
                <ul className="imessage-features">
                  <li>Search and request by just texting a title</li>
                  <li>Get recommendations based on your taste</li>
                  <li>Track what's downloading and when it's ready</li>
                  <li>Manages conversation history and context</li>
                  <li>Configurable prompts and behavior via config</li>
                </ul>
                <p className="imessage-tagline">Works from any iMessage conversation. That's it.</p>
              </div>
            </Reveal>
            <Reveal delay={0.2}>
              <div className="imessage-phone-desktop">
                <IMessageChat />
              </div>
            </Reveal>
          </div>
        </section>
        <Reveal delay={0.2}>
          <div className="imessage-phone-mobile">
            <IMessageChat />
          </div>
        </Reveal>

        {/* ── How it works ── */}
        <section className="how-section">
          <MarqueeHeading>How it works</MarqueeHeading>
          <Reveal>
            <p className="section-desc">
              BluePopcorn connects your AI to Seerr via MCP.
              It sees your library, knows what's available, and handles every API call. You talk naturally.
            </p>
          </Reveal>
          <div className="workflow-grid">
            <Reveal delay={0.1}>
              <div className="workflow-card">
                <div className="workflow-label">Request</div>
                <div className="workflow">"Add the new Dune" → search → confirm → request → <span className="neon-text">done</span></div>
              </div>
            </Reveal>
            <Reveal delay={0.2}>
              <div className="workflow-card">
                <div className="workflow-label">Discover</div>
                <div className="workflow">"Something like Severance" → find similar → browse → pick → <span className="neon-text">requested</span></div>
              </div>
            </Reveal>
            <Reveal delay={0.3}>
              <div className="workflow-card">
                <div className="workflow-label">Status</div>
                <div className="workflow">"What's downloading?" → check queue → <span className="neon-text">3 active, 2 pending</span></div>
              </div>
            </Reveal>
          </div>
        </section>

      </main>

      {/* ── Footer ── */}
      <footer ref={footerRef} className={`site-footer ${footerVisible ? 'footer-flicker' : ''}`}>
        <img src="/favicon.svg" alt="BluePopcorn logo" width="32" height="32" className="footer-logo" />
        <div className="footer-neon neon-text">BluePopcorn</div>
        <p className="footer-tagline">MCP server + iMessage bot for Seerr</p>
        <div className="footer-credits">
          <p>
            3D models: <a href="https://www.fab.com/listings/729d32f2-29ad-4359-8fdd-ea752adbd7d3" target="_blank" rel="noopener noreferrer">FitzDude</a> · <a href="https://www.fab.com/listings/42bdd427-9309-46b2-a30b-e95053116a5f" target="_blank" rel="noopener noreferrer">Glowbox3D</a>
          </p>
        </div>
      </footer>
    </>
  )
}

export default App
