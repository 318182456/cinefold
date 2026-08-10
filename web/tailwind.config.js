/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#10b981',
          hover: '#059669',
        },
      },
    },
  },
  plugins: [],
}
