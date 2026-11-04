/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        gray: {
          950: '#101316', 900: '#181d21', 800: '#2b3339', 700: '#404b54',
          600: '#87939e', 500: '#96a2ad', 400: '#b0bac3', 300: '#cbd3da',
          200: '#e1e6eb', 100: '#f2f5f7', 50: '#f8fafb',
        },
        brand: {
          DEFAULT: '#3ecf9b',
          hover: '#56deb0',
        },
      },
    },
  },
  plugins: [],
}
