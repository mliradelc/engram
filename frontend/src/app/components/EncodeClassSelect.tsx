import { sv } from "./synapse";
import { AUTO_CLASS, ENCODE_CLASSES, isEncodeClass } from "./encodeClasses";
import type { EncodeClass } from "../../types";

interface EncodeClassSelectProps {
  /** undefined = Auto. */
  value: EncodeClass | undefined;
  onChange: (value: EncodeClass | undefined) => void;
  disabled?: boolean;
  /** Accessible name, e.g. "Codec class for Show - S01E02.mkv". */
  label: string;
  title?: string;
}

/** Codec-class picker for a Retry: Auto (classifier) plus the four forceable classes. */
export function EncodeClassSelect({ value, onChange, disabled, label, title }: EncodeClassSelectProps) {
  return (
    <select
      data-testid="encode-class-select"
      aria-label={label}
      title={title}
      value={value ?? AUTO_CLASS}
      disabled={disabled}
      onChange={(e) => onChange(isEncodeClass(e.target.value) ? e.target.value : undefined)}
      style={{
        fontFamily: sv.mono,
        fontSize: 11,
        letterSpacing: "0.04em",
        color: disabled ? sv.inkFaint : sv.ink,
        background: sv.bg3,
        border: `1px solid ${sv.lineMid}`,
        borderRadius: 0,
        padding: "5px 6px",
        maxWidth: 170,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      <option value={AUTO_CLASS}>Auto (classifier)</option>
      {ENCODE_CLASSES.map((cls) => (
        <option key={cls} value={cls}>
          {cls}
        </option>
      ))}
    </select>
  );
}
