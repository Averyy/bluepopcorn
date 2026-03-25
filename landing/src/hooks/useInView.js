import { useRef, useEffect, useState } from 'react'

export function useInView({ threshold = 0.2, repeat = false } = {}) {
  const ref = useRef(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    if (!ref.current) return
    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setInView(true)
        if (!repeat) obs.disconnect()
      } else if (repeat) {
        setInView(false)
      }
    }, { threshold })
    obs.observe(ref.current)
    return () => obs.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- options are static per call site
  }, [])
  return [ref, inView]
}
