import * as React from 'react'
import { cn } from '@/lib/utils'

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => (
    <input
      ref={ref}
      type={type}
      className={cn(
        'h-8 w-full rounded-md border border-border bg-bg-input px-2.5 text-base text-fg',
        'placeholder:text-fg-faint',
        'transition-colors duration-fast',
        'hover:border-border-strong',
        'focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/40',
        'disabled:cursor-not-allowed disabled:opacity-40',
        className
      )}
      {...props}
    />
  )
)
Input.displayName = 'Input'

export { Input }
