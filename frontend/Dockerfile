FROM node:22-alpine AS build
WORKDIR /ui
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY index.html vite.config.ts ./
COPY src ./src
RUN npm run build
FROM nginx:1.27-alpine
COPY --from=build /ui/dist /usr/share/nginx/html
