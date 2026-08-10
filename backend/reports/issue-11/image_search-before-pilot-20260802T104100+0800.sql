--
-- PostgreSQL database dump
--

\restrict HZveNBaAxZtkL9yJoT2hicgQYY7noPGf1xnngUIMdr47ez2UE2y3PQHNJXRXLRf

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: product_images; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.product_images (
    id integer NOT NULL,
    model_number character varying(100) NOT NULL,
    image_path character varying(255) NOT NULL,
    vector public.vector(1024) NOT NULL,
    original_path text,
    oss_path text,
    image_order integer DEFAULT 0,
    is_primary boolean DEFAULT false,
    created_at timestamp without time zone DEFAULT now(),
    content_hash character varying(64)
);


ALTER TABLE public.product_images OWNER TO postgres;

--
-- Name: TABLE product_images; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.product_images IS '产品图片表，存储图片路径和 DashScope 1024 维图像向量';


--
-- Name: product_images_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.product_images_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.product_images_id_seq OWNER TO postgres;

--
-- Name: product_images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.product_images_id_seq OWNED BY public.product_images.id;


--
-- Name: products; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.products (
    model_number character varying(100) NOT NULL,
    photographer_file character varying(255) NOT NULL,
    alibaba_product_url character varying(500) NOT NULL,
    category character varying(100) NOT NULL,
    spec_cn_reference text,
    spec_cn text,
    spec_en text,
    product_size character varying(200),
    package_size character varying(200),
    price_1688 numeric(10,2),
    fob_price_tier1 numeric(10,2),
    fob_price_tier2 numeric(10,2),
    fob_price_tier3 numeric(10,2),
    intl_platform_price numeric(10,2),
    competitor_price numeric(10,2),
    ref_link_1 character varying(500),
    ref_link_2 character varying(500),
    ref_link_3 character varying(500),
    intl_platform_url character varying(500),
    intl_platform_url_1 character varying(500),
    intl_platform_url_2 character varying(500),
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.products OWNER TO postgres;

--
-- Name: TABLE products; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.products IS '电子产品配件主表（相机肩带、挂绳等）';


--
-- Name: product_images id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.product_images ALTER COLUMN id SET DEFAULT nextval('public.product_images_id_seq'::regclass);


--
-- Data for Name: product_images; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.product_images (id, model_number, image_path, vector, original_path, oss_path, image_order, is_primary, created_at, content_hash) FROM stdin;
\.


--
-- Data for Name: products; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.products (model_number, photographer_file, alibaba_product_url, category, spec_cn_reference, spec_cn, spec_en, product_size, package_size, price_1688, fob_price_tier1, fob_price_tier2, fob_price_tier3, intl_platform_price, competitor_price, ref_link_1, ref_link_2, ref_link_3, intl_platform_url, intl_platform_url_1, intl_platform_url_2, created_at, updated_at) FROM stdin;
\.


--
-- Name: product_images_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.product_images_id_seq', 3, true);


--
-- Name: product_images product_images_image_path_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_image_path_key UNIQUE (image_path);


--
-- Name: product_images product_images_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_pkey PRIMARY KEY (id);


--
-- Name: products products_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (model_number);


--
-- Name: idx_product_images_model_number; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_product_images_model_number ON public.product_images USING btree (model_number);


--
-- Name: idx_product_images_vector_hnsw; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_product_images_vector_hnsw ON public.product_images USING hnsw (vector public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: uq_product_images_content_hash; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_product_images_content_hash ON public.product_images USING btree (content_hash);


--
-- Name: product_images product_images_model_number_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_model_number_fkey FOREIGN KEY (model_number) REFERENCES public.products(model_number) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict HZveNBaAxZtkL9yJoT2hicgQYY7noPGf1xnngUIMdr47ez2UE2y3PQHNJXRXLRf

