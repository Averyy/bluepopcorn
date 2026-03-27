import { useEffect, useState } from 'react'
import { useInView } from '../hooks/useInView.js'

export function MarqueeHeading({ children, className = '' }) {
  const [ref, inView] = useInView({ threshold: 0.05 })
  const [play, setPlay] = useState(false)
  const text = typeof children === 'string' ? children : ''

  useEffect(() => {
    if (!inView || play) return
    const timer = setTimeout(() => setPlay(true), 400)
    return () => clearTimeout(timer)
  }, [inView, play])

  const words = text.split(' ')
  let charIndex = 0

  return (
    <h2 ref={ref} className={`marquee-heading ${className} ${play ? 'marquee-play' : ''}`}>
      {words.map((word, wi) => {
        const letters = word.split('').map((char) => {
          const idx = charIndex++
          return (
            <span key={idx} className="marquee-letter" style={{ animationDelay: `${idx * 0.04}s` }}>
              {char}
            </span>
          )
        })
        if (wi < words.length - 1) charIndex++
        return (
          <span key={`w${wi}`} style={{ whiteSpace: 'nowrap' }}>
            {letters}
            {wi < words.length - 1 && (
              <span className="marquee-letter" style={{ animationDelay: `${(charIndex - 1) * 0.04}s` }}>{'\u00A0'}</span>
            )}
          </span>
        )
      })}
    </h2>
  )
}
