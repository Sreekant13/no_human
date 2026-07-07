import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Shared markdown renderer for agent prose, spec fields (approach/test_plan/
// verification), and PR/review bodies — renders real tables, code blocks,
// bold/headers instead of showing raw markdown syntax as plain text.
export default function Markdown({ children }) {
  if (!children) return null;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
          code: ({ inline, className, children, ...props }) =>
            inline ? (
              <code className="md-inline-code" {...props}>{children}</code>
            ) : (
              <code className={className} {...props}>{children}</code>
            ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
