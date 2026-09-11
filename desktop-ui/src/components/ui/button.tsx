import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  // 所有变体都有明确 hover/active/disabled/focus 反馈，圆角 ≤8px，无重阴影
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium ' +
    'transition-colors duration-fast ' +
    'disabled:pointer-events-none disabled:opacity-40 ' +
    'active:translate-y-[0.5px] ' +
    '[&_svg]:pointer-events-none [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default:
          'bg-primary text-primary-fg hover:bg-primary-hover active:bg-primary',
        secondary:
          'bg-bg-card-hover text-fg border border-border hover:bg-border-subtle hover:border-border-strong active:bg-bg-card',
        outline:
          'bg-transparent text-fg-muted border border-border hover:bg-bg-card-hover hover:text-fg hover:border-border-strong active:bg-bg-card',
        ghost:
          'bg-transparent text-fg-muted hover:bg-bg-card-hover hover:text-fg active:bg-bg-card',
        danger:
          'bg-danger text-white hover:bg-danger-hover active:bg-danger',
      },
      size: {
        sm: 'h-7 px-2 text-sm [&_svg]:size-3.5',
        default: 'h-8 px-3 text-base [&_svg]:size-4',
        lg: 'h-9 px-4 text-md [&_svg]:size-4',
        icon: 'size-8 [&_svg]:size-4',
        'icon-sm': 'size-7 [&_svg]:size-3.5',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'default' },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
  loading?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading, children, disabled, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={disabled || loading}
        {...props}
      >
        {loading ? (
          <>
            <Loader2 className="animate-spin" />
            {children}
          </>
        ) : (
          children
        )}
      </Comp>
    )
  }
)
Button.displayName = 'Button'

export { Button, buttonVariants }
