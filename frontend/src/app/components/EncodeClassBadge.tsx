import { sv } from "./synapse";

interface EncodeClassBadgeProps {
  /** null/undefined: the fixed settings ran (classifier off) or the class is not known yet. */
  codecClass: string | null | undefined;
  forced?: boolean;
}

const chip = {
  fontFamily: sv.mono,
  fontSize: 9,
  fontWeight: 700,
  letterSpacing: "0.14em",
  textTransform: "uppercase" as const,
  padding: "1px 5px",
  whiteSpace: "nowrap" as const,
};

/** The codec class an encode ran (or runs) as, with a FORCED chip when an operator chose it. */
export function EncodeClassBadge({ codecClass, forced }: EncodeClassBadgeProps) {
  const known = Boolean(codecClass);
  return (
    <span
      data-testid="encode-class-badge"
      title={known ? `Codec class: ${codecClass}` : "Codec class not known yet, or fixed settings (classifier off)"}
      style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}
    >
      <span
        style={{
          ...chip,
          color: known ? sv.cyan : sv.inkFaint,
          border: `1px solid ${known ? `${sv.cyan}4d` : sv.lineMid}`,
          textTransform: "none",
          letterSpacing: "0.04em",
        }}
      >
        {known ? codecClass : "—"}
      </span>
      {forced && (
        <span
          data-testid="encode-class-forced"
          style={{ ...chip, color: sv.magentaHi, border: `1px solid ${sv.magenta}66` }}
        >
          FORCED
        </span>
      )}
    </span>
  );
}
