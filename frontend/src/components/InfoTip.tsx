import {
  type CSSProperties,
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Info } from "lucide-react";

/**
 * Toggletip: a button that opens a short explanation. The bubble is portalled so
 * a scrolling table never clips it; screen readers get the same text through the
 * polite live region next to the button.
 */
export function InfoTip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({});
  const button = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const id = useId();
  useLayoutEffect(() => {
    if (!open || !button.current || !bubble.current) return;
    const anchor = button.current.getBoundingClientRect();
    const box = bubble.current.getBoundingClientRect();
    const gap = 8;
    const left = Math.min(
      Math.max(gap, anchor.left + anchor.width / 2 - box.width / 2),
      window.innerWidth - box.width - gap,
    );
    const below = anchor.bottom + gap;
    const top =
      below + box.height > window.innerHeight - gap &&
      anchor.top - box.height - gap > gap
        ? anchor.top - box.height - gap
        : below;
    setStyle({ left, top, visibility: "visible" });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onPointer = (e: PointerEvent) => {
      const target = e.target as Node;
      if (
        !button.current?.contains(target) &&
        !bubble.current?.contains(target)
      )
        close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        button.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);
  return (
    <span className="info-tip">
      <button
        ref={button}
        type="button"
        className="info-tip-button"
        aria-expanded={open}
        aria-label={t("aboutMetric", { label })}
        onClick={() => {
          setStyle({});
          setOpen((o) => !o);
        }}
      >
        <Info size={14} aria-hidden="true" />
      </button>
      <span role="status" className="sr-only">
        {open ? children : null}
      </span>
      {open &&
        createPortal(
          <div
            ref={bubble}
            id={id}
            className="info-tip-bubble"
            aria-hidden="true"
            style={{ visibility: "hidden", ...style }}
          >
            <strong>{label}</strong>
            {children}
          </div>,
          document.body,
        )}
    </span>
  );
}
