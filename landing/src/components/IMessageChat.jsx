import { useRef, useEffect, useState } from 'react'
import { useInView } from '../hooks/useInView.js'
import { CHAT_MESSAGES } from '../data/chatMessages.js'

export function IMessageChat() {
  const [ref, inView] = useInView()
  const [messages, setMessages] = useState([])
  const timeoutsRef = useRef([])
  const msgRef = useRef(null)
  const stepRef = useRef(0)
  const msgIdRef = useRef(0)

  useEffect(() => {
    if (!inView || stepRef.current > 0) return

    let cancelled = false
    const msgs = []

    function next() {
      if (cancelled) return
      if (stepRef.current >= CHAT_MESSAGES.length) {
        stepRef.current = 0
        timeoutsRef.current = [setTimeout(next, 1500)]
        return
      }

      const entry = CHAT_MESSAGES[stepRef.current]

      if (entry.who === 'bot') {
        const typingId = ++msgIdRef.current
        msgs.push({ who: 'typing', id: typingId })
        setMessages([...msgs])
        trimOldMessages()

        timeoutsRef.current.push(setTimeout(() => {
          if (cancelled) return
          msgs[msgs.length - 1] = { who: 'typing-out', id: typingId }
          setMessages([...msgs])

          timeoutsRef.current.push(setTimeout(() => {
            if (cancelled) return
            msgs.pop()
            msgs.push({ ...entry, id: ++msgIdRef.current })
            setMessages([...msgs])
            trimOldMessages()
            stepRef.current++
            timeoutsRef.current.push(setTimeout(next, 1200 + Math.random() * 800))
          }, 250))
        }, 1400 + Math.random() * 800))
      } else {
        msgs.push({ ...entry, id: ++msgIdRef.current })
        setMessages([...msgs])
        trimOldMessages()
        stepRef.current++
        timeoutsRef.current.push(setTimeout(next, 1000 + Math.random() * 600))
      }
    }

    function trimOldMessages() {
      // Keep more messages than visible — CSS overflow:hidden clips them.
      // Only trim once well past the visible area to avoid popping.
      if (msgs.length > 12) {
        msgs.splice(0, msgs.length - 10)
        setMessages([...msgs])
      }
    }

    timeoutsRef.current.push(setTimeout(next, 500))

    return () => {
      cancelled = true
      timeoutsRef.current.forEach(clearTimeout)
      timeoutsRef.current = []
    }
  }, [inView])

  return (
    <div ref={ref} className="imessage-phone" role="img" aria-label="Animated demo of iMessage conversation with BluePopcorn bot">
      <div className="imessage-notch" />
      <div className="imessage-messages" ref={msgRef}>
        {messages.map((m) => (
          m.who === 'typing' ? (
            <div key={m.id} className="msg msg-bot msg-appear">
              <div className="typing-dots"><span /><span /><span /></div>
            </div>
          ) : m.who === 'typing-out' ? (
            <div key={m.id} className="msg msg-bot msg-fade-out">
              <div className="typing-dots"><span /><span /><span /></div>
            </div>
          ) : (
            <div key={m.id} className={`msg msg-${m.who} msg-appear`}>{m.text}</div>
          )
        ))}
      </div>
      <div className="imessage-input">
        <div className="imessage-input-field">iMessage</div>
      </div>
    </div>
  )
}
