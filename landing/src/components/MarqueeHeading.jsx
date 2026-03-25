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

  return (
    <h2 ref={ref} className={`marquee-heading ${className} ${play ? 'marquee-play' : ''}`}>
      {text.split('').map((char, i) => (
        <span
          key={i}
          className="marquee-letter"
          style={{ animationDelay: `${i * 0.04}s` }}
        >
          {char === ' ' ? '\u00A0' : char}
        </span>
      ))}
    </h2>
  )
}
