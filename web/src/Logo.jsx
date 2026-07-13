// Legion logo — an app-icon-style mark (like Claude/Gemini): a blue badge with
// three ascending white chevrons (legion ranks / a formation advancing).
export function LegionLogo({ size = 30 }) {
  return (
    <svg className="legion-logo" width={size} height={size} viewBox="0 0 32 32"
         role="img" aria-label="no_human" fill="none">
      <defs>
        <linearGradient id="legion-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#4C9AFF" />
          <stop offset="1" stopColor="#0C66E4" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="url(#legion-grad)" />
      <g stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="9,13 16,9 23,13" />
        <polyline points="9,18.5 16,14.5 23,18.5" />
        <polyline points="9,24 16,20 23,24" />
      </g>
    </svg>
  );
}
