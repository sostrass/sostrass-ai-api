/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Paleta oficial do iOS Dark Mode
        ios: {
          bg: '#000000',           // Fundo principal
          card: '#1C1C1E',         // Fundo de cards sólidos
          gray: '#8E8E93',         // Textos secundários
          blue: '#0A84FF',         // Botões primários Apple
          separator: '#38383A',    // Linhas divisórias
        },
        shopee: {
          orange: '#EE4D2D',       // Pra manter a identidade da origem
        }
      },
      animation: {
        // Animação suave para quando a IA estiver pensando
        shimmer: 'shimmer 2s linear infinite',
      },
      keyframes: {
        shimmer: {
          '0%': { backgroundPosition: '-1000px 0' },
          '100%': { backgroundPosition: '1000px 0' },
        }
      }
    },
  },
  plugins: [],
}
